# Premium Typography System

> Evermind 自研排版引擎，融合现代 CJK 排版最佳实践与西方设计体系。

## 适用节点
- Builder
- Polisher

## 核心规范

### 1. 容器内边距（Content Guard Rails）
所有主要内容块必须遵守：
```css
.section-content {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 clamp(1.5rem, 5vw, 6rem);
}
```

### 2. 中文排版基线
```css
body {
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  line-height: 1.8;           /* CJK 需要更大行高 */
  letter-spacing: 0.04em;     /* 微调字间距 */
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

h1, h2, h3, h4 {
  line-height: 1.35;
  letter-spacing: 0.02em;
  font-weight: 700;
}

p {
  max-width: 68ch;     /* 阅读舒适度 */
  margin-bottom: 1.5em;
  color: rgba(255,255,255,0.85);    /* 深色主题柔和白 */
}
```

### 3. 标题层级系统
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

### 4. 列表视觉层级
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

### 5. 强制检查清单
构建网页时必须验证：
- [ ] 所有文字内容不贴左边缘（min padding 1.5rem）
- [ ] 中文正文 line-height >= 1.7
- [ ] 标题文字使用渐变或强调色
- [ ] 序号使用装饰框/badge 而非裸数字
- [ ] body 文字颜色不是纯白（使用 rgba 降低亮度）
- [ ] max-width 限制防止单行过长
