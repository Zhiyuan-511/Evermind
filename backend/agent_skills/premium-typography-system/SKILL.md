# Premium Typography System

> Evermind's in-house typography spec — modern CJK best practices fused with Western design systems.

## Applies to
- Builder
- Polisher

## Core spec

### 1. Container padding (content guard rails)
Every major content block must obey:
```css
.section-content {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 clamp(1.5rem, 5vw, 6rem);
}
```

### 2. Chinese typography baseline
```css
body {
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  line-height: 1.8;           /* CJK requires more line-height */
  letter-spacing: 0.04em;     /* fine-tune letter spacing */
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

h1, h2, h3, h4 {
  line-height: 1.35;
  letter-spacing: 0.02em;
  font-weight: 700;
}

p {
  max-width: 68ch;     /* reading comfort */
  margin-bottom: 1.5em;
  color: rgba(255,255,255,0.85);    /* soft white on dark theme */
}
```

### 3. Heading hierarchy system
```css
.section-number {
  display: inline-block;
  font-size: 0.75rem;
  font-weight: 800;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--accent, #a855f7);
  margin-bottom: 0.5rem;
  padding: 0.2rem 0.8rem;
  border: 1px solid currentColor;
  border-radius: 2rem;
}

h2.section-title {
  font-size: clamp(2rem, 4vw, 3.5rem);
  margin-bottom: 1rem;
  background: linear-gradient(135deg, #fff 40%, rgba(168,85,247,0.8));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.section-subtitle {
  font-size: clamp(1rem, 2vw, 1.25rem);
  color: rgba(255,255,255,0.5);
  font-weight: 400;
  margin-bottom: 2rem;
}
```

### 4. List visual hierarchy
```css
.feature-list {
  list-style: none;
  padding: 0;
  display: grid;
  gap: 0.75rem;
}

.feature-list li {
  position: relative;
  padding-left: 1.75rem;
  line-height: 1.7;
  color: rgba(255,255,255,0.75);
}

.feature-list li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0.65em;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #a855f7);
}
```

### 5. Hard checklist
When building a page, you must verify:
- [ ] No text touches the left edge (min padding 1.5rem)
- [ ] Chinese body copy uses line-height >= 1.7
- [ ] Heading text uses a gradient or accent colour
- [ ] Section numbers use a decorative frame/badge, not a bare digit
- [ ] Body text colour is not pure white (use rgba to soften brightness)
- [ ] `max-width` is set to prevent overly long lines
