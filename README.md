Aegis_backend/
├── app/
│   ├── main.py                         # FastAPI 启动入口，相当于 AssetsApplication
│   │
│   ├── common/                         # 通用返回体、常量、业务异常、枚举
│   │   ├── response.py
│   │   ├── exceptions.py
│   │   ├── constants.py
│   │   └── enums.py
│   │
│   ├── config/                         # 数据库、Redis、日志、跨域、环境变量
│   │   ├── settings.py
│   │   ├── database.py
│   │   ├── redis.py
│   │   ├── celery_config.py
│   │   ├── cors_config.py
│   │   └── telemetry_config.py
│   │
│   ├── controller/                     # API 接口层
│   │   ├── conversation_controller.py  # 会话、消息、SSE
│   │   ├── run_controller.py           # 任务进度、任务详情、重试
│   │   ├── calendar_controller.py      # 日历查询、日历草稿
│   │   ├── mail_controller.py          # 收到、草稿、已发送邮件
│   │   ├── approval_controller.py      # 批准、拒绝待确认操作
│   │   └── memory_controller.py        # 长期记忆管理
│   │
│   ├── entity/                         # SQLAlchemy 数据库实体
│   │   ├── user_entity.py
│   │   ├── conversation_entity.py
│   │   ├── message_entity.py
│   │   ├── run_entity.py
│   │   ├── tool_call_entity.py
│   │   ├── approval_item_entity.py
│   │   ├── calendar_draft_entity.py
│   │   ├── mail_snapshot_entity.py
│   │   ├── mail_draft_entity.py
│   │   ├── sent_mail_entity.py
│   │   ├── todo_draft_entity.py
│   │   └── memory_entity.py
│   │
│   ├── mapper/                         # Entity / Param / VO 转换
│   │   ├── conversation_mapper.py
│   │   ├── run_mapper.py
│   │   ├── calendar_mapper.py
│   │   ├── mail_mapper.py
│   │   ├── approval_mapper.py
│   │   └── memory_mapper.py
│   │
│   ├── param/                          # 请求参数模型（Pydantic）
│   │   ├── conversation_create_param.py
│   │   ├── message_send_param.py
│   │   ├── calendar_draft_param.py
│   │   ├── mail_draft_param.py
│   │   ├── approval_param.py
│   │   └── memory_param.py
│   │
│   ├── repository/                     # 数据访问层，封装 CRUD / SQL
│   │   ├── conversation_repository.py
│   │   ├── run_repository.py
│   │   ├── calendar_repository.py
│   │   ├── mail_repository.py
│   │   ├── approval_repository.py
│   │   └── memory_repository.py
│   │
│   ├── service/                        # 业务逻辑层
│   │   ├── conversation_service.py
│   │   ├── run_service.py
│   │   ├── calendar_service.py
│   │   ├── mail_service.py
│   │   ├── approval_service.py
│   │   ├── memory_service.py
│   │   ├── agent_service.py            # Agent 编排入口
│   │   ├── model_router_service.py     # 意图、模型、工具选择
│   │   └── policy_service.py           # 风险、权限、确认策略
│   │
│   ├── tool/                           # 项目特有：MCP 工具层
│   │   ├── tool_registry.py
│   │   ├── tool_gateway.py
│   │   ├── calendar_mcp_client.py
│   │   └── email_mcp_client.py
│   │
│   ├── worker/                         # Celery 异步任务
│   │   ├── mail_worker.py              # 邮件摘要、起草回复
│   │   ├── agent_worker.py
│   │   └── retry_worker.py
│   │
│   ├── security/                       # 项目特有：安全防护
│   │   ├── auth_security.py
│   │   ├── permission_security.py
│   │   ├── guardrail_security.py
│   │   ├── pii_security.py
│   │   └── confirmation_token.py
│   │
│   ├── util/                           # 工具类
│   │   ├── jwt_util.py
│   │   ├── date_util.py
│   │   ├── hash_util.py
│   │   ├── id_util.py
│   │   └── redact_util.py
│   │
│   └── vo/                             # 返回给前端的响应模型
│       ├── conversation_vo.py
│       ├── run_vo.py
│       ├── calendar_vo.py
│       ├── mail_vo.py
│       ├── approval_vo.py
│       └── memory_vo.py
│
├── migrations/                         # Alembic 数据库迁移
├── tests/
│   ├── controller/
│   ├── service/
│   ├── repository/
│   ├── tool/
│   └── integration/
│
├── .env.example
├── pyproject.toml
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
└── README.md