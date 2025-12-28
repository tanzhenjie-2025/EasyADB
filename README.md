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
- ADB工具（需添加到系统环境变量）
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
   
   pip install -r requirements.txt  # 需自行创建包含依赖的requirements.txt
   ```

3. 配置环境
   - 修改`EasyADB/settings.py`中的Redis配置（`REDIS_HOST`、`REDIS_PORT`等）
   - 确保ADB工具可正常调用（执行`adb devices`测试）

4. 初始化数据库
   ```bash
   python manage.py migrate
   python manage.py createsuperuser  # 创建管理员账户
   ```

5. 启动服务
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

6. 访问系统
   - 打开浏览器访问 `http://127.0.0.1:8000`
   - 使用管理员账户登录后即可开始管理设备与任务


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
│   ├── settings.py        # 项目配置
│   ├── urls.py            # 主路由配置
│   └── ...
├── manage.py              # Django管理脚本
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
1. **修改应用adb_manager**
   - 有关设备操作需要用日志记录并将字段补充到设备操作日志ADBDeviceOperationLog
   
   
## 许可证

[MIT](LICENSE) 


## 致谢

- 依赖的开源工具：Django、Celery、Redis、ADB
- 前端基础组件（如适用）：Bootstrap、jQuery等