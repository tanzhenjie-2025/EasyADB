# mycelery/config.py 完整配置（覆盖原有内容）
# 核心：指定使用Redis作为broker和result_backend，而非默认的RabbitMQ
broker_url = 'redis://127.0.0.1:6379/0'  # Redis消息队列
result_backend = 'redis://127.0.0.1:6379/0'  # 任务结果存储
broker_connection_retry_on_startup = True  # 启动时重试连接（避免Redis未启动崩溃）
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
timezone = 'Asia/Shanghai'  # 和Django时区一致
enable_utc = False
result_expires = 3600  # 任务结果1小时后过期