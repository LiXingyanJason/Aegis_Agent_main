# 知识来源卡片测试（6.6）

在aegis_frontend目录运行`npm test`；`npm run build`另外验证全部Vue SFC生产编译。
使用已有Vue／Pinia／Vite及Vue自带server-renderer，未增加测试依赖或调用真实API。
测试在Vite SSR中渲染真实KnowledgeSourceCard，验证转义、失效展示及版本／位置／编号；
Pinia测试仅替换API模块，覆盖SSE去重后的刷新、请求失败清除旧片段与过期请求不覆盖新结果。
来源详情快照一致性以纯函数验证；真正弹窗交互、OIDC登录与云服务流程仍需手工浏览器联调。
不把SSR和Mock测试称为真实端到端。主后端先升级015，更多部署与场景见Worker文档15及接口文档篇末第9章。
