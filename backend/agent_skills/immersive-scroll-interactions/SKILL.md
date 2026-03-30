# Immersive Scroll Interactions

> Evermind 自研沉浸式滚动交互引擎 — 无依赖、纯 CSS + Vanilla JS 实现电影级滚动体验。

## 适用节点
- Builder
- Polisher

## 核心交互系统

### 1. Scroll-Driven 进度指示器
```css
.scroll-progress {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 3px;
  background: var(--accent-gradient, linear-gradient(90deg, #a855f7, #3b82f6));
  transform-origin: left;
  transform: scaleX(0);
  z-index: 9999;
}
@supports (animation-timeline: scroll()) {
  .scroll-progress {
    animation: fill-progress linear;
    animation-timeline: scroll(root);
  }
  @keyframes fill-progress {
    to { transform: scaleX(1); }
  }
}
```

### 2. 交错式卡片入场
```css
.stagger-grid > * {
  opacity: 0;
  transform: translateY(30px);
  transition: opacity 0.6s ease, transform 0.6s ease;
}
.stagger-grid > *.visible { opacity: 1; transform: translateY(0); }

/* 延迟序列 */
.stagger-grid > *:nth-child(1) { transition-delay: 0.0s; }
.stagger-grid > *:nth-child(2) { transition-delay: 0.1s; }
.stagger-grid > *:nth-child(3) { transition-delay: 0.2s; }
.stagger-grid > *:nth-child(4) { transition-delay: 0.3s; }
.stagger-grid > *:nth-child(5) { transition-delay: 0.4s; }
.stagger-grid > *:nth-child(6) { transition-delay: 0.5s; }
```
```javascript
const staggerObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
    }
  });
}, { threshold: 0.1 });

document.querySelectorAll('.stagger-grid > *').forEach(el => staggerObserver.observe(el));
```

### 3. 数字计数器动画
```javascript
function animateCounter(element, target, duration = 2000) {
  let start = 0;
  const startTime = performance.now();
  function update(timestamp) {
    const elapsed = timestamp - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
    const current = Math.round(eased * target);
    element.textContent = current.toLocaleString();
    if (progress < 1) requestAnimationFrame(update);
  }
  requestAnimationFrame(update);
}

// 触发（仅在可见时开始）
const counterObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      const target = parseInt(entry.target.dataset.count || '0');
      animateCounter(entry.target, target);
      counterObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.5 });

document.querySelectorAll('[data-count]').forEach(el => counterObserver.observe(el));
```

### 4. 顺滑锚点导航
```css
html { scroll-behavior: smooth; }

.nav-link {
  position: relative;
  transition: color 0.3s ease;
}
.nav-link::after {
  content: '';
  position: absolute;
  bottom: -2px;
  left: 0;
  width: 0%;
  height: 2px;
  background: var(--accent-primary);
  transition: width 0.3s ease;
}
.nav-link:hover::after,
.nav-link.active::after {
  width: 100%;
}
```

### 5. 吸附式导航栏
```css
.sticky-nav {
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(20px);
  background: rgba(10, 10, 15, 0.85);
  border-bottom: 1px solid rgba(255,255,255,0.06);
  transition: background 0.3s ease, box-shadow 0.3s ease;
}
.sticky-nav.scrolled {
  background: rgba(10, 10, 15, 0.95);
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
}
```
```javascript
const nav = document.querySelector('.sticky-nav');
if (nav) {
  window.addEventListener('scroll', () => {
    nav.classList.toggle('scrolled', window.scrollY > 60);
  }, { passive: true });
}
```

### 6. 检查清单
- [ ] 使用 IntersectionObserver 而非 scroll 事件（性能更好）
- [ ] 所有动画使用 GPU 友好属性（transform, opacity）
- [ ] 滚动监听器使用 `{ passive: true }`
- [ ] 计数器动画使用 requestAnimationFrame
- [ ] 导航栏使用 backdrop-filter 半透明效果
- [ ] CSS Scroll-Driven Animations 作渐进增强（@supports 检测）
