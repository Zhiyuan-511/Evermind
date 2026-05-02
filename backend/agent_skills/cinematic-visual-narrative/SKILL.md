# Cinematic Visual Narrative

> Evermind's in-house cinematic-visual-narrative engine — guidance for full-screen immersive page design.

## Applies to
- Builder
- Analyst
- UIDesign

## Core design principles

### 1. Full-screen hero section
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

### 2. Parallax layer design
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

### 3. Progressive content reveal
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
// Auto reveal via Intersection Observer
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
    }
  });
}, { threshold: 0.15, rootMargin: '0px 0px -50px 0px' });

document.querySelectorAll('.reveal-section').forEach(el => observer.observe(el));
```

### 4. Cinematic color system
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

### 5. Premium card component
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

### 6. Design checklist
- [ ] Hero must fill the viewport (min-height: 100vh)
- [ ] Background images go through brightness + overlay treatment
- [ ] Sections are visually separated by gradient transitions or distinct backgrounds
- [ ] Reveal animations use Intersection Observer
- [ ] Card elements use a glassmorphism effect
- [ ] Imagery uses CSS composition (gradients + SVG) rather than external image links
- [ ] Overall palette is unified — avoid bright pure-saturation colours
