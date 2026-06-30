## v4.0.4 Release Notes

### **Feature**

- 支持 `CUSTOM_FOOTER_HTML` 环境变量注入自定义页脚 HTML（与 `CUSTOM_HEAD_HTML` 对称）

### **BugFixes**

- 修复 Tab 切换时封面图报错
- 修复首页封面图加载异常
- 修复主题切换时 fetch 请求因旧缓存失败
- 修复订阅源分组折叠导致的 JavaScript 崩溃
- 优化源菜单弹出逻辑，消除闪烁
- 强化新闻抓取容错：异常保护、HTML 解析兜底、图片缓存一致性

### **Refactor**

- `web_server.py` 路由逻辑简化，移除冗余中间件
- 新增自动化测试：UI 契约测试、回归测试

### **Image**

- `ghcr.io/rayyume/raynews:v4.0.4`
- `ghcr.io/rayyume/raynews:latest`
- `ghcr.io/rayyume/raynews:dev`
