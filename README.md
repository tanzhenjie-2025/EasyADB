
# EasyADB

基于Django的ADB设备管理与任务自动化编排平台，简化Android设备的ADB连接管理、脚本执行及多步骤任务编排流程。


## 项目简介

EasyADB是一个面向Android设备的Web化管理工具，通过直观的界面实现ADB设备集中管理、自动化脚本执行和多步骤任务编排，适用于需要批量操作Android设备的场景（如自动化测试、设备监控、批量部署等）。


## 核心功能

### 1. ADB设备管理（`adb_manager`）
- 支持两种设备连接方式：**序列号连接**（USB或无线调试）和**IP+端口连接**（局域网无线ADB）
- 实时监控设备在线状态（基于Redis缓存）
- 自动/手动重连设备，记录连接日志
- 设备信息管理（名称、关联用户、启用状态等）

### 2. 脚本任务管理（`script_center`）
- 管理可在设备上执行的脚本（支持普通Python脚本和Airtest脚本）
- 配置脚本路径、Python解释器路径、日志存储路径
- 异步执行脚本任务（基于Celery），支持任务停止与状态跟踪
- 详细记录执行日志（标准输出、错误信息、执行时长）

### 3. 任务编排（`task_orchestration`）
- 创建多步骤的自动化流程，按顺序组合多个脚本任务
- 支持子任务执行顺序调整、运行时长限制配置
- 跟踪整个编排流程的执行状态（总步骤数、已完成步骤数）
- 记录每个步骤的详细执行日志（命令、输出、耗时等）

### 4. 定时任务（`task_scheduler`）
- 基于Cron表达式设置任务定时执行计划
- 支持关联编排任务，指定执行设备（或自动选择在线设备）
- 记录定时任务的执行历史、下次执行时间
- 支持手动触发定时任务执行


## 技术栈

| 模块         | 技术/工具                  | 作用说明                     |
|--------------|---------------------------|------------------------------|
| 后端框架     | Django (Python)           | Web应用核心框架，处理请求与业务逻辑 |
| 异步任务     | Celery                    | 处理耗时任务（脚本执行、任务编排） |
| 缓存/状态存储 | Redis                     | 存储设备状态、任务进程信息       |
| 数据库       | SQLite (默认)             | 存储设备配置、任务定义等结构化数据 |
| 前端         | HTML + CSS + JavaScript   | 基础Web界面渲染               |
| 设备通信     | ADB (Android Debug Bridge) | 与Android设备交互的核心工具    |
| 进程管理     | psutil                    | 监控与管理脚本执行进程         |


## 快速开始

### 环境要求
- Python 3.8+
- ADB工具（需添加到系统环境变量，或在.env中指定路径）
- Redis服务（用于缓存与Celery broker）

### 安装步骤

1. 克隆项目
   ```bash
   git clone <项目仓库地址>
   cd EasyADB
   ```

2. 创建虚拟环境并安装依赖
   ```bash
   python -m venv venv
   # Windows激活环境
   venv\Scripts\activate
   # Linux/Mac激活环境
   source venv/bin/activate
   
   # 安装依赖（需先创建requirements.txt，模板见下方）
   pip install -r requirements.txt
   ```

3. 创建环境配置文件（.env）
   - 在项目根目录创建 `.env` 文件（**重要：该文件不纳入代码仓库，需自行创建**）
   - 复制以下模板并根据自身环境修改配置（关键路径、密钥等）：
   ```ini
   # Django核心配置
   SECRET_KEY=请替换为自定义的Django密钥（可通过Django官方工具生成）
   DEBUG=True
   ALLOWED_HOSTS=127.0.0.1,localhost  # 多个值用逗号分隔

   # 数据库配置（SQLite文件名）
   DB_FILENAME=easy_adb_db.sqlite3

   # Redis配置
   REDIS_HOST=127.0.0.1
   REDIS_PORT=6379
   REDIS_DB=0

   # 国际化配置
   LANGUAGE_CODE=zh-hans
   TIME_ZONE=Asia/Shanghai
   USE_TZ=False

   # CORS配置（开发环境）
   CORS_ALLOW_ALL_ORIGINS=True

   # Celery配置
   CELERY_RESULT_EXPIRES=3600
   CELERY_TASK_SERIALIZER=json
   CELERY_RESULT_SERIALIZER=json

   # ADB相关配置
   ADB_PATH=C:\Users\你的用户名\AppData\Local\Android\Sdk\platform-tools\adb.exe  # Windows示例，Linux/Mac请替换为adb绝对路径
   ADB_DEFAULT_WIRELESS_PORT=5555  # 无线ADB默认端口
   ADB_COMMAND_TIMEOUT=15  # ADB命令执行超时时间（秒）

   # Script Center 相关配置
   # 日志文件路径
   SCRIPT_LOG_FILE=script_execution.log
   # 最近执行日志展示数量
   SCRIPT_RECENT_LOGS_LIMIT=10
   # 停止任务时的等待时间（秒）
   SCRIPT_STOP_WAIT_TIME=8
   # 进程优雅终止等待时间（秒）
   SCRIPT_PROCESS_TERMINATE_WAIT=3
   # Redis停止信号有效期（秒）
   SCRIPT_REDIS_STOP_FLAG_EXPIRE=60
   # Python路径警告关键词
   SCRIPT_PYTHON_WARNING_KEYWORD=WindowsApps

   # Task Orchestration 编排任务相关配置
   # 编排任务日志文件路径
   ORCH_LOG_FILE=orchestration_execution.log
   # 最近执行日志展示数量
   ORCH_RECENT_LOGS_LIMIT=10
   # 步骤执行超时缓冲时间（秒，基础超时+此值作为最大等待时间）
   ORCH_STEP_TIMEOUT_BUFFER=10
   # 进程终止前等待时间（秒）
   ORCH_PROCESS_TERMINATE_WAIT=1
   # Redis哈希表名称（编排任务运行进程）
   ORCH_REDIS_PROCESS_HASH=orch_running_processes
   # Celery任务终止方式（True=强制终止，False=优雅终止）
   ORCH_CELERY_TERMINATE_FORCE=True
   # 手机号校验位数
   ORCH_MOBILE_VALID_LENGTH=11
   ```

4. 配置项目读取.env文件
   - 确保 `EasyADB/settings.py` 中已配置读取 `.env` 文件（示例代码）：
   ```python
   import os
   from dotenv import load_dotenv

   # 加载.env文件
   load_dotenv()

   # 从.env读取配置
   SECRET_KEY = os.getenv("SECRET_KEY")
   DEBUG = os.getenv("DEBUG") == "True"
   ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS").split(",")
   # 其他配置（Redis、ADB路径等）同理
   ```

5. 初始化数据库
   ```bash
   python manage.py migrate
   python manage.py createsuperuser  # 创建管理员账户
   ```

6. 启动服务
   - 启动Django Web服务
     ```bash
     python manage.py runserver
     ```
   - 启动Celery worker（处理异步任务）
     ```bash
     celery -A mycelery.main worker --loglevel=info
     ```
   - 启动Celery beat（处理定时任务）
     ```bash
     celery -A mycelery.main beat --loglevel=info
     ```

7. 访问系统
   - 打开浏览器访问 `http://127.0.0.1:8000`
   - 使用管理员账户登录后即可开始管理设备与任务


## 依赖文件模板（requirements.txt）
在项目根目录创建 `requirements.txt`，复制以下内容：
```txt
django==5.1.3
celery==5.3.6
redis==5.0.1
python-dotenv==1.0.1  # 用于读取.env文件
croniter==1.4.1       # 解析Cron表达式
psutil==5.9.8         # 进程管理
django-cors-headers==4.3.0  # CORS跨域支持
```


## 项目结构

```
EasyADB/
├── adb_manager/           # ADB设备管理模块
│   ├── models.py          # 设备模型定义
│   ├── tasks.py           # 设备检查/重连的Celery任务
│   ├── views.py           # 设备管理相关视图
│   └── ...
├── script_center/         # 脚本任务管理模块
│   ├── models.py          # 脚本任务与执行日志模型
│   ├── views.py           # 脚本执行相关视图
│   └── ...
├── task_orchestration/    # 任务编排模块
│   ├── models.py          # 编排任务与步骤模型
│   └── ...
├── task_scheduler/        # 定时任务模块
│   ├── models.py          # 定时任务模型（基于Cron）
│   └── ...
├── mycelery/              # Celery配置
│   ├── main.py            # Celery实例初始化
│   └── tasks.py           # 核心异步任务定义
├── common/                # 公共组件
│   └── models.py          # 基础抽象模型（如时间字段）
├── EasyADB/               # 项目核心配置
│   ├── settings.py        # 项目配置（读取.env文件）
│   ├── urls.py            # 主路由配置
│   └── ...
├── manage.py              # Django管理脚本
├── .env                   # 环境配置文件（不纳入仓库，需自行创建）
├── requirements.txt       # 依赖清单
└── ...
```


## 使用说明

1. **添加ADB设备**
   - 进入「设备管理」页面，点击「添加设备」
   - 选择连接方式：填写序列号（优先）或IP+端口
   - 启用设备监控后，系统将自动维护设备连接状态

2. **创建脚本任务**
   - 进入「脚本中心」，配置脚本路径、Python解释器路径
   - 选择「Airtest模式」可支持Airtest脚本的特殊执行方式
   - 保存后可在「执行任务」页面选择设备运行脚本

3. **编排多步骤任务**
   - 进入「任务编排」，创建编排任务并添加子任务步骤
   - 配置步骤顺序、运行时长限制
   - 选择设备执行编排任务，实时查看各步骤执行状态

4. **设置定时任务**
   - 进入「定时任务」，关联已创建的编排任务
   - 通过Cron表达式配置执行计划（如每天8点执行）
   - 可指定执行设备或选择“自动选择在线设备”


## 代码修改注意事项

### 1. ADB设备管理模块（adb_manager）
- 所有设备操作（连接、重连、断开、修改配置等）必须补充日志到 `ADBDeviceOperationLog` 模型，确保操作可追溯；
- 修改设备连接逻辑时，需同步更新 `models.py` 中 `connect_identifier` 和 `adb_connect_str` 属性，保证Redis中设备状态键名一致性；
- 新增设备验证规则时，需在 `forms.py` 的 `clean` 方法中补充校验逻辑，避免无效设备配置；
- 调整设备状态监控频率时，需修改Celery定时任务的执行间隔（`tasks.py` 中 `check_device_status` 任务的 `schedule` 参数）。

### 2. 脚本任务管理模块（script_center）
- 调整脚本执行命令时，需强化 `subprocess.run` 的参数安全过滤，避免命令注入风险；
- 修改日志存储路径时，需确保目标目录有写入权限，并同步更新 `TaskExecutionLog` 模型的 `log_path` 默认值；
- 新增脚本类型（如Shell脚本）时，需在 `models.py` 中扩展 `ScriptType` 枚举，并在执行逻辑中补充对应的命令拼接规则；
- 调整任务停止逻辑时，需同步更新Redis中停止信号的过期时间（对应 `.env` 中 `SCRIPT_REDIS_STOP_FLAG_EXPIRE` 配置）。

### 3. 任务编排模块（task_orchestration）
- 新增流程控制逻辑（如条件分支、循环）时，需扩展 `OrchestrationStep` 模型的字段（如添加分支条件、循环次数）；
- 修改步骤执行超时逻辑时，需兼顾 `.env` 中 `ORCH_STEP_TIMEOUT_BUFFER` 配置，避免硬编码超时值；
- 调整Celery任务终止方式时，需同步修改 `.env` 中 `ORCH_CELERY_TERMINATE_FORCE` 配置，或在代码中兼容该配置项。

### 4. 定时任务模块（task_scheduler）
- 修改Cron表达式解析逻辑时，需兼容 `.env` 中时区配置（`TIME_ZONE`），避免定时任务执行时间偏移；
- 新增设备选择策略（如按设备分组执行）时，需在 `ScheduleTask` 模型中补充分组关联字段，并在执行逻辑中添加筛选规则。

### 5. 通用配置
- 修改 `.env` 配置后，需重启Django和Celery服务才能生效；
- 所有敏感配置（如SECRET_KEY）必须通过 `.env` 管理，禁止硬编码到代码中；
- 扩展 `settings.py` 时，需保持配置读取逻辑统一（优先从 `.env` 读取，兜底默认值）。


## 许可证

[MIT](LICENSE) 


## 致谢

- 依赖的开源工具：Django、Celery、Redis、ADB
- 前端基础组件（如适用）：Bootstrap、jQuery等
- 环境配置依赖：python-dotenv
```

