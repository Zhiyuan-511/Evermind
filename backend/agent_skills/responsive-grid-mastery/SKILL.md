# Responsive Grid Mastery

> Evermind's in-house responsive layout system — an advanced approach built on CSS Grid + container queries.

## Applies to
- Builder

## Core layout patterns

### 1. Fluid grid system
```css
.grid-auto {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
  gap: clamp(1rem, 3vw, 2.5rem);
}

.grid-2 { grid-template-columns: repeat(auto-fit, minmax(min(100%, 480px), 1fr)); }
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); }
.grid-4 { grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); }
```

### 2. Advanced layout — featured area
```css
.featured-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto auto;
  gap: 1.5rem;
}
.featured-grid .featured-item:first-child {
  grid-row: 1 / 3;   /* large hero on the left */
}
@media (max-width: 768px) {
  .featured-grid {
    grid-template-columns: 1fr;
  }
  .featured-grid .featured-item:first-child {
    grid-row: auto;
  }
}
```

### 3. Fluid typography
```css
:root {
  --step--1: clamp(0.75rem, 0.65rem + 0.5vw, 0.875rem);
  --step-0: clamp(0.875rem, 0.75rem + 0.625vw, 1.125rem);
  --step-1: clamp(1.125rem, 0.95rem + 0.875vw, 1.5rem);
  --step-2: clamp(1.5rem, 1.2rem + 1.5vw, 2.25rem);
  --step-3: clamp(2rem, 1.5rem + 2.5vw, 3.5rem);
  --step-4: clamp(2.5rem, 1.8rem + 3.5vw, 5rem);
}

body { font-size: var(--step-0); }
h1 { font-size: var(--step-4); }
h2 { font-size: var(--step-3); }
h3 { font-size: var(--step-2); }
h4 { font-size: var(--step-1); }
small, .caption { font-size: var(--step--1); }
```

### 4. Spacing system
```css
:root {
  --space-2xs: clamp(0.25rem, 0.2rem + 0.25vw, 0.375rem);
  --space-xs: clamp(0.5rem, 0.4rem + 0.5vw, 0.75rem);
  --space-s: clamp(0.75rem, 0.6rem + 0.75vw, 1.125rem);
  --space-m: clamp(1rem, 0.8rem + 1vw, 1.5rem);
  --space-l: clamp(1.5rem, 1.2rem + 1.5vw, 2.25rem);
  --space-xl: clamp(2rem, 1.5rem + 2.5vw, 3.5rem);
  --space-2xl: clamp(3rem, 2rem + 5vw, 6rem);
  --space-3xl: clamp(4rem, 3rem + 5vw, 8rem);
}

section { padding-block: var(--space-3xl); }
.container { padding-inline: var(--space-l); max-width: 1200px; margin-inline: auto; }
```

### 5. Mobile-first breakpoints
```css
/* Base: Mobile (320px+) */
/* sm: >=640px */
@media (min-width: 640px)  { /* tablet portrait */ }
/* md: >=768px */
@media (min-width: 768px)  { /* tablet landscape */ }
/* lg: >=1024px */
@media (min-width: 1024px) { /* desktop */ }
/* xl: >=1280px */
@media (min-width: 1280px) { /* large screens */ }
```

### 6. Checklist
- [ ] Every grid uses `auto-fill` / `auto-fit` + `minmax()` for fluid sizing
- [ ] Type sizes use `clamp()` for fluid scaling
- [ ] Spacing is centralised through CSS custom properties
- [ ] Test every breakpoint between 320px and 1920px
- [ ] Images preserve their ratio with `aspect-ratio`
- [ ] Containers centre via `max-width` + `margin-inline: auto`
