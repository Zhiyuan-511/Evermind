# Immersive Scroll Interactions

> Evermind's in-house immersive scroll-interaction engine — dependency-free, pure CSS + vanilla JS, delivering a cinematic scroll experience.

## Applies to
- Builder
- Polisher

## Core interaction systems

### 1. Scroll-driven progress indicator
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

### 2. Staggered card entrance
```css
.stagger-grid > * {
  opacity: 0;
  transform: translateY(30px);
  transition: opacity 0.6s ease, transform 0.6s ease;
}
.stagger-grid > *.visible { opacity: 1; transform: translateY(0); }

/* delay sequence */
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

### 3. Numeric counter animation
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

// trigger (only start when visible)
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

### 4. Smooth anchor navigation
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

### 5. Sticky navigation bar
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

### 6. Checklist
- [ ] Use IntersectionObserver instead of scroll events (better performance)
- [ ] All animations use GPU-friendly properties (transform, opacity)
- [ ] Scroll listeners use `{ passive: true }`
- [ ] Counter animation uses requestAnimationFrame
- [ ] Nav bar uses backdrop-filter for the translucent effect
- [ ] CSS scroll-driven animations are progressive enhancement (gated with `@supports`)
