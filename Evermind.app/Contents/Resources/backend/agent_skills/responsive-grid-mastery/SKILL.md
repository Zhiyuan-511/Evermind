# Responsive Grid Mastery

> Evermind 自研自适应布局系统 — 基于 CSS Grid + Container Queries 的高级响应式方案。

## 适用节点
- Builder

## 核心布局模式

### 1. 流式网格系统
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

### 2. 高级布局 — 特色区域
```css
.featured-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto auto;
  gap: 1.5rem;
}
.featured-grid .featured-item:first-child {
  grid-row: 1 / 3;   /* 左侧大图 */
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

### 3. 流式排版 (Fluid Typography)
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

### 4. 间距系统
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

### 5. 移动端优先断点
```css
/* Base: Mobile (320px+) */
/* sm: >=640px */
@media (min-width: 640px)  { /* 平板竖屏 */ }
/* md: >=768px */
@media (min-width: 768px)  { /* 平板横屏 */ }
/* lg: >=1024px */
@media (min-width: 1024px) { /* 桌面 */ }
/* xl: >=1280px */
@media (min-width: 1280px) { /* 大屏 */ }
```

### 6. 检查清单
- [ ] 所有网格使用 `auto-fill`/`auto-fit` + `minmax()` 实现自适应
- [ ] 文字大小使用 `clamp()` 流式缩放
- [ ] 间距使用 CSS custom properties 统一管理
- [ ] 测试 320px ~ 1920px 所有断点表现
- [ ] 图片使用 `aspect-ratio` 保持比例
- [ ] 容器使用 `max-width` + `margin-inline: auto` 居中
