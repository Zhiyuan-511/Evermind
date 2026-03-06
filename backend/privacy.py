"""
Evermind Backend — Privacy / Desensitization Engine (脱敏处理)
Regex-based PII masking with reversible mask/unmask support.
References: Microsoft Presidio patterns, LLM Guard by Protect AI.
"""

import hashlib
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.privacy")


# ─────────────────────────────────────────────
# Built-in PII Patterns
# ─────────────────────────────────────────────
BUILTIN_PATTERNS = {
    # Chinese phone numbers
    "phone_cn": {
        "regex": r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "label": "手机号",
        "label_en": "Phone",
        "mask": "***PHONE***",
        "example": "13812345678",
    },
    # International phone (E.164-like)
    "phone_intl": {
        "regex": r"\+\d{1,3}[-.\s]?\d{4,14}",
        "label": "国际电话",
        "label_en": "Intl Phone",
        "mask": "***INTL_PHONE***",
    },
    # Email addresses
    "email": {
        "regex": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        "label": "邮箱",
        "label_en": "Email",
        "mask": "***EMAIL***",
        "example": "user@example.com",
    },
    # Chinese ID card (18 digits)
    "id_card_cn": {
        "regex": r"(?<!\d)\d{17}[\dXx](?!\d)",
        "label": "身份证号",
        "label_en": "ID Card",
        "mask": "***ID_CARD***",
    },
    # Bank card numbers (16-19 digits)
    "bank_card": {
        "regex": r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4,7}(?!\d)",
        "label": "银行卡号",
        "label_en": "Bank Card",
        "mask": "***BANK_CARD***",
    },
    # IPv4 addresses
    "ipv4": {
        "regex": r"(?<!\d)(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}(?!\d)",
        "label": "IP地址",
        "label_en": "IP Address",
        "mask": "***IP***",
    },
    # API keys (sk-..., key-..., etc.)
    "api_key": {
        "regex": r"(?:sk|key|token|api[_-]?key)[-_]?[a-zA-Z0-9]{20,}",
        "label": "API密钥",
        "label_en": "API Key",
        "mask": "***API_KEY***",
    },
    # Passwords in common formats
    "password_field": {
        "regex": r'(?:password|passwd|pwd|secret|密码)\s*[=:]\s*["\']?(\S{4,})["\']?',
        "label": "密码",
        "label_en": "Password",
        "mask": "***PASSWORD***",
    },
    # Absolute file paths (macOS/Linux)
    "file_path": {
        "regex": r"(?:/Users/|/home/|/root/)[^\s\"'<>|]+",
        "label": "文件路径",
        "label_en": "File Path",
        "mask": "***/MASKED_PATH/***",
    },
    # URLs with credentials
    "url_with_creds": {
        "regex": r"https?://[^:]+:[^@]+@[^\s]+",
        "label": "带密码URL",
        "label_en": "URL with credentials",
        "mask": "***CRED_URL***",
    },
    # SSH private keys
    "ssh_key": {
        "regex": r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----",
        "label": "SSH密钥",
        "label_en": "SSH Key",
        "mask": "***SSH_KEY***",
    },
}


class PrivacyMasker:
    """
    Reversible PII masking engine.

    Usage:
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn", "email", "api_key"])
        masked_text, restore_map = masker.mask(original_text)
        # ... send masked_text to AI ...
        restored_text = masker.unmask(ai_response, restore_map)
    """

    def __init__(
        self,
        enabled: bool = True,
        patterns: Optional[List[str]] = None,
        custom_patterns: Optional[List[Dict]] = None,
        exclude_node_types: Optional[List[str]] = None,
        show_indicator: bool = True,
    ):
        self.enabled = enabled
        self.show_indicator = show_indicator
        self.exclude_node_types = set(exclude_node_types or [
            "localshell", "fileread", "filewrite"
        ])

        # Build active pattern list
        self._patterns: List[Dict] = []
        active_names = patterns or list(BUILTIN_PATTERNS.keys())
        for name in active_names:
            if name in BUILTIN_PATTERNS:
                p = BUILTIN_PATTERNS[name].copy()
                p["name"] = name
                p["_compiled"] = re.compile(p["regex"], re.IGNORECASE)
                self._patterns.append(p)

        # Add user-defined custom patterns
        if custom_patterns:
            for cp in custom_patterns:
                try:
                    compiled = re.compile(cp.get("regex", ""), re.IGNORECASE)
                    self._patterns.append({
                        "name": cp.get("name", f"custom_{len(self._patterns)}"),
                        "regex": cp["regex"],
                        "label": cp.get("label", "自定义"),
                        "label_en": cp.get("label_en", "Custom"),
                        "mask": cp.get("mask", "***CUSTOM***"),
                        "_compiled": compiled,
                    })
                except re.error as e:
                    logger.warning(f"Invalid custom pattern: {cp.get('regex')}: {e}")

        logger.info(f"PrivacyMasker initialized: enabled={enabled}, "
                    f"patterns={len(self._patterns)}, "
                    f"exclude_nodes={self.exclude_node_types}")

    def mask(self, text: str, node_type: str = "") -> Tuple[str, Dict[str, str]]:
        """
        Mask PII in text. Returns (masked_text, restore_map).
        The restore_map maps mask_token → original_value for later unmasking.
        """
        if not self.enabled or not text:
            return text, {}

        # Skip masking for excluded node types (local-only operations)
        if node_type and node_type in self.exclude_node_types:
            return text, {}

        restore_map: Dict[str, str] = {}
        masked_text = text
        stats = {}

        for pattern in self._patterns:
            compiled = pattern["_compiled"]
            matches = list(compiled.finditer(masked_text))
            if not matches:
                continue

            stats[pattern["name"]] = len(matches)

            # Replace each match with a unique token
            for match in reversed(matches):  # reversed to preserve positions
                original = match.group(0)
                # Generate unique token so we can unmask later
                token_id = hashlib.md5(
                    f"{original}:{uuid.uuid4().hex[:8]}".encode()
                ).hexdigest()[:8]
                mask_token = f"[{pattern['mask']}:{token_id}]"
                restore_map[mask_token] = original
                masked_text = (
                    masked_text[:match.start()]
                    + mask_token
                    + masked_text[match.end():]
                )

        if stats:
            logger.info(f"Masked PII: {stats}")

        return masked_text, restore_map

    def unmask(self, text: str, restore_map: Dict[str, str]) -> str:
        """
        Restore masked tokens in AI response back to original values.
        """
        if not restore_map or not text:
            return text

        result = text
        restored_count = 0
        for mask_token, original in restore_map.items():
            if mask_token in result:
                result = result.replace(mask_token, original)
                restored_count += 1

        if restored_count:
            logger.info(f"Unmasked {restored_count} PII tokens")

        return result

    def get_patterns_info(self) -> List[Dict]:
        """Return pattern metadata for frontend display."""
        return [
            {
                "name": p["name"],
                "label": p["label"],
                "label_en": p.get("label_en", p["label"]),
                "mask": p["mask"],
                "example": p.get("example", ""),
            }
            for p in self._patterns
        ]

    def test_mask(self, sample_text: str) -> Dict:
        """Test masking on sample text (for frontend preview)."""
        masked, restore_map = self.mask(sample_text)
        return {
            "original": sample_text,
            "masked": masked,
            "pii_found": len(restore_map),
            "tokens": list(restore_map.keys()),
            "can_unmask": self.unmask(masked, restore_map) == sample_text,
        }


# ─────────────────────────────────────────────
# Global instance (configurable at runtime)
# ─────────────────────────────────────────────
_global_masker: Optional[PrivacyMasker] = None


def get_masker(settings: Dict = None) -> PrivacyMasker:
    """Get or create the global PrivacyMasker from settings."""
    global _global_masker

    if settings:
        privacy_cfg = settings.get("privacy", {})
        _global_masker = PrivacyMasker(
            enabled=privacy_cfg.get("enabled", True),
            patterns=None,  # Use all built-in by default
            custom_patterns=privacy_cfg.get("customPatterns", []),
            exclude_node_types=privacy_cfg.get("excludeNodeTypes", [
                "localshell", "fileread", "filewrite"
            ]),
            show_indicator=privacy_cfg.get("showIndicator", True),
        )
    elif _global_masker is None:
        _global_masker = PrivacyMasker()

    return _global_masker


def update_masker_settings(settings: Dict):
    """Update the global masker with new settings from frontend."""
    global _global_masker
    _global_masker = None  # Force re-creation
    return get_masker(settings)
