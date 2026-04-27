'use client';
/**
 * Evermind Welcome Wizard (v6.2 — maintainer 2026-04-20)
 *
 * First-run onboarding shown when localStorage.evermind_onboarded_v62 is unset.
 * 2 branches, any skip button exits:
 *   1. "Pick a Quick-Start Template"  → loads graph + pre-fills goal → user hits Run
 *   2. "Configure My Own Key"         → opens SettingsModal
 *
 * Design: 2025 AI coding tools have dropped multi-step wizards; this is the
 * single-input + template cards pattern (Bolt.new / V0). No shared-key / cost
 * exposure — templates run with the user's own configured API key.
 */
import { useState } from 'react';
import { getQuickStartTemplates, type TemplateDef } from './TemplateGallery';

interface WelcomeWizardProps {
    lang: 'zh' | 'en';
    onPickTemplate: (tpl: TemplateDef) => void;
    onConfigureKey: () => void;
    onSkip: () => void;
}

// v6.2b (maintainer 2026-04-20): bumped so users who saw the prior demo-key
// variant see the new template-based wizard again. Bump further if future
// wizard content changes materially.
const STORAGE_KEY = 'evermind_onboarded_v62b';

export function shouldShowWelcomeWizard(): boolean {
    if (typeof window === 'undefined') return false;
    try {
        return window.localStorage.getItem(STORAGE_KEY) !== '1';
    } catch {
        return false;
    }
}

export function markWelcomeWizardSeen(): void {
    if (typeof window === 'undefined') return;
    try {
        window.localStorage.setItem(STORAGE_KEY, '1');
    } catch {
        /* ignore */
    }
}

export default function WelcomeWizard({ lang, onPickTemplate, onConfigureKey, onSkip }: WelcomeWizardProps) {
    const [step, setStep] = useState<'choose' | 'templates'>('choose');
    const templates = getQuickStartTemplates();

    const t = (zh: string, en: string) => (lang === 'zh' ? zh : en);

    const handleTemplateClick = (tpl: TemplateDef) => {
        onPickTemplate(tpl);
        markWelcomeWizardSeen();
        onSkip();
    };

    const handleSkip = () => {
        markWelcomeWizardSeen();
        onSkip();
    };

    const handleConfigureKey = () => {
        markWelcomeWizardSeen();
        onConfigureKey();
        onSkip();
    };

    return (
        <div style={{
            position: 'fixed', inset: 0, zIndex: 9000,
            background: 'rgba(0, 0, 0, 0.78)', backdropFilter: 'blur(10px)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 20,
        }}>
            <div style={{
                background: 'linear-gradient(135deg, #1a1d24 0%, #14161c 100%)',
                border: '1px solid rgba(168, 85, 247, 0.3)',
                borderRadius: 16,
                boxShadow: '0 20px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(168,85,247,0.15)',
                padding: 36,
                maxWidth: step === 'templates' ? 880 : 520,
                width: '100%',
                maxHeight: '90vh',
                overflow: 'auto',
                position: 'relative',
            }}>
                <button
                    onClick={handleSkip}
                    title={t('跳过（可稍后从帮助菜单重开）', 'Skip (reopen from Help later)')}
                    style={{
                        position: 'absolute', top: 14, right: 16,
                        background: 'transparent', border: 'none', cursor: 'pointer',
                        color: '#9BA1AC', fontSize: 20, padding: 4,
                    }}
                >
                    ✕
                </button>

                {step === 'choose' && (
                    <>
                        <div style={{ fontSize: 44, marginBottom: 12 }}>🧠</div>
                        <h2 style={{ fontSize: 22, fontWeight: 700, color: '#fff', marginBottom: 6 }}>
                            {t('欢迎来到 Evermind', 'Welcome to Evermind')}
                        </h2>
                        <p style={{ color: '#9BA1AC', fontSize: 13, lineHeight: 1.6, marginBottom: 24 }}>
                            {t(
                                '多智能体流水线，从目标到可跑的产物。先挑一个模板试试，还是直接配置 API Key？',
                                'Multi-agent DAG pipeline: goal to working artifact. Start with a template, or configure your API key first?'
                            )}
                        </p>

                        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                            <button
                                onClick={() => setStep('templates')}
                                style={{
                                    padding: '14px 20px', borderRadius: 10,
                                    border: '1px solid rgba(168, 85, 247, 0.5)',
                                    background: 'linear-gradient(135deg, rgba(168,85,247,0.18), rgba(88,140,255,0.12))',
                                    color: '#fff', fontSize: 14, fontWeight: 600,
                                    cursor: 'pointer', textAlign: 'left',
                                }}
                            >
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                    <span style={{ fontSize: 20 }}>⚡</span>
                                    <div>
                                        <div>{t('挑一个模板开始（推荐）', 'Pick a Quick-Start Template (recommended)')}</div>
                                        <div style={{ fontSize: 11, color: '#9BA1AC', fontWeight: 400, marginTop: 2 }}>
                                            {t('8 个精选模板，点击自动填充目标 + 流水线', '8 curated templates · auto-fills goal + pipeline graph')}
                                        </div>
                                    </div>
                                </div>
                            </button>

                            <button
                                onClick={handleConfigureKey}
                                style={{
                                    padding: '14px 20px', borderRadius: 10,
                                    border: '1px solid rgba(255,255,255,0.1)',
                                    background: 'rgba(255,255,255,0.03)',
                                    color: '#fff', fontSize: 14, fontWeight: 600,
                                    cursor: 'pointer', textAlign: 'left',
                                }}
                            >
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                    <span style={{ fontSize: 20 }}>🔑</span>
                                    <div>
                                        <div>{t('先配置我的 API Key', 'Configure My API Key First')}</div>
                                        <div style={{ fontSize: 11, color: '#9BA1AC', fontWeight: 400, marginTop: 2 }}>
                                            {t('直连 kimi / 阿里 / 豆包 / OpenAI / 中转站', 'Direct kimi / DashScope / Doubao / OpenAI / relay')}
                                        </div>
                                    </div>
                                </div>
                            </button>
                        </div>

                        <button
                            onClick={handleSkip}
                            style={{
                                marginTop: 20, fontSize: 11, color: '#9BA1AC',
                                background: 'transparent', border: 'none', cursor: 'pointer',
                                textDecoration: 'underline', textUnderlineOffset: 3,
                            }}
                        >
                            {t('稍后再说', 'Maybe later')}
                        </button>
                    </>
                )}

                {step === 'templates' && (
                    <>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
                            <button
                                onClick={() => setStep('choose')}
                                style={{
                                    background: 'transparent', border: 'none', cursor: 'pointer',
                                    color: '#9BA1AC', fontSize: 14, padding: 4,
                                }}
                            >
                                ← {t('返回', 'Back')}
                            </button>
                            <h2 style={{ fontSize: 18, fontWeight: 700, color: '#fff', margin: 0 }}>
                                {t('挑一个模板 — 点击即可开始', 'Pick a template — click to start')}
                            </h2>
                        </div>

                        <div style={{
                            display: 'grid',
                            gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
                            gap: 12,
                        }}>
                            {templates.map(tpl => (
                                <button
                                    key={tpl.key}
                                    onClick={() => handleTemplateClick(tpl)}
                                    style={{
                                        textAlign: 'left',
                                        padding: 14,
                                        borderRadius: 10,
                                        border: '1px solid rgba(255,255,255,0.08)',
                                        background: 'rgba(255,255,255,0.03)',
                                        color: '#fff',
                                        cursor: 'pointer',
                                        transition: 'all 150ms ease',
                                    }}
                                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(168,85,247,0.4)'; }}
                                    onMouseLeave={e => { e.currentTarget.style.borderColor = 'rgba(255,255,255,0.08)'; }}
                                >
                                    <div style={{ fontSize: 28, marginBottom: 8 }}>{tpl.cover_emoji || tpl.icon}</div>
                                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
                                        {lang === 'zh' ? tpl.title_zh : tpl.title_en}
                                    </div>
                                    <div style={{ fontSize: 11, color: '#9BA1AC', lineHeight: 1.4, marginBottom: 8 }}>
                                        {lang === 'zh' ? tpl.desc_zh : tpl.desc_en}
                                    </div>
                                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', fontSize: 10, color: '#9BA1AC' }}>
                                        {tpl.tags.slice(0, 3).map(tag => (
                                            <span key={tag} style={{
                                                padding: '2px 6px', borderRadius: 4,
                                                background: 'rgba(255,255,255,0.05)',
                                            }}>{tag}</span>
                                        ))}
                                        {tpl.est_duration_sec && (
                                            <span style={{
                                                padding: '2px 6px', borderRadius: 4,
                                                background: 'rgba(168,85,247,0.1)',
                                                color: '#d4a8ff',
                                            }}>~{Math.round(tpl.est_duration_sec / 60)} min</span>
                                        )}
                                    </div>
                                </button>
                            ))}
                        </div>

                        <div style={{
                            marginTop: 16, fontSize: 11, color: '#9BA1AC',
                            padding: '8px 12px', borderRadius: 6,
                            background: 'rgba(255,255,255,0.03)',
                            border: '1px solid rgba(255,255,255,0.06)',
                        }}>
                            💡 {t(
                                '模板会自动填充目标到聊天框 + 在画布里生成节点流水线。点 Run 按钮启动时会使用你在 Settings 里配置的 API Key。',
                                'Template pre-fills the goal + draws the pipeline on canvas. Click Run — it uses the API key you configure in Settings.'
                            )}
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}
