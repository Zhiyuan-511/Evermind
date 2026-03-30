# Cinematic Visual Narrative

> Evermind 自研电影级视觉叙事引擎 — 全屏沉浸式页面设计指导。

## 适用节点
- Builder
- Analyst
- UIDesign

## 核心设计原则

### 1. 全屏英雄区（Hero Section）
```css
.hero {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.hero-bg {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
  filter: brightness(0.4) saturate(1.2);
}

.hero-overlay {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(0,0,0,0.3) 0%,
    rgba(0,0,0,0.1) 40%,
    rgba(0,0,0,0.6) 100%
  );
}

.hero-content {
  position: relative;
  z-index: 2;
  text-align: center;
  max-width: 800px;
  padding: 2rem;
}
```

### 2. 视差层设计
```css
@supports (animation-timeline: scroll()) {
  .parallax-section {
    overflow: hidden;
  }
  .parallax-bg {
    transform: translateY(-15%);
    animation: parallax-scroll linear;
    animation-timeline: view();
    animation-range: entry 0% exit 100%;
  }
  @keyframes parallax-scroll {
    from { transform: translateY(-15%); }
    to   { transform: translateY(15%); }
  }
}
```

### 3. 渐进式内容揭示
```css
.reveal-section {
  opacity: 0;
  transform: translateY(40px);
  transition: opacity 0.8s ease, transform 0.8s ease;
}
.reveal-section.visible {
  opacity: 1;
  transform: translateY(0);
}
```
```javascript
// Intersection Observer 自动揭示
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
    }
  });
}, { threshold: 0.15, rootMargin: '0px 0px -50px 0px' });

document.querySelectorAll('.reveal-section').forEach(el => observer.observe(el));
```

### 4. 电影级色彩系统
```css
:root {
  --bg-primary: #0a0a0f;
  --bg-secondary: #13131a;
  --bg-card: rgba(255,255,255,0.03);
  --text-primary: rgba(255,255,255,0.92);
  --text-secondary: rgba(255,255,255,0.65);
  --text-muted: rgba(255,255,255,0.35);
  --accent-primary: #a855f7;
  --accent-secondary: #6366f1;
  --accent-gradient: linear-gradient(135deg, #a855f7 0%, #6366f1 50%, #3b82f6 100%);
  --glass: rgba(255,255,255,0.04);
  --glass-border: rgba(255,255,255,0.08);
}
```

### 5. 高品质卡片组件
```css
.premium-card {
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  padding: 2rem;
  backdrop-filter: blur(12px);
  transition: transform 0.3s ease, box-shadow 0.3s ease;
}
.premium-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 20px 40px rgba(0,0,0,0.3);
  border-color: rgba(168,85,247,0.3);
}
```

### 6. 设计检查清单
- [ ] 首屏必须全屏（min-height: 100vh）
- [ ] 背景图使用 brightness + overlay 处理
- [ ] 每个 section 之间有结构性分隔（渐变过渡或差异背景）
- [ ] 使用 Intersection Observer 实现渐入动效
- [ ] 卡片元素使用 glassmorphism 效果
- [ ] 图片使用 CSS 构图（渐变 + SVG 组合）而非外部链接
- [ ] 整体色调统一，不使用鲜艳纯色
