# GTFS Import Pipeline

一个高效的 **GTFS Static 数据导入工具**，将澳洲城市的公交时刻表数据下载、解析、存入 SQLite 数据库。

支持 **Sydney（TfNSW）** 和 **Melbourne（PTV）** 两大城市的火车/电车/巴士/渡轮全模式数据。

## 为什么用 Python 而不是 Swift

- **系统工具链**：直接调用 macOS 原生的 `curl`、`unzip`，下载/解压速度极快
- **零中间层**：Python 标准库的 `csv` + `sqlite3`（C 实现），解析和写入接近原生速度
- **实测效果**：2000 万条记录，3 分钟以内完成全流程

## 环境要求

- **Python 3.7+**（无需额外 pip 安装）
- **curl**（macOS 预装）
- **unzip**（macOS 预装）

验证环境：
```bash
python3 --version   # Python 3.7+
which curl unzip    # 应该都有输出
```

## 获取 API Key

### Melbourne（PTV）— 无需 Key

PTV 开放数据可直接下载，无需注册。

### Sydney（TfNSW）— 需要 API Key

1. 访问 [Transport for NSW Open Data Hub](https://opendata.transport.nsw.gov.au/)
2. 注册账号，创建应用
3. 申请订阅 **Timetables Complete GTFS** 数据产品
4. 获取 Primary API Key

## 使用方法

### Melbourne（开箱即用）

```bash
python3 Scripts/import_gtfs.py melbourne
```

首次运行会自动下载 GTFS ZIP（约 260MB），后续运行跳过下载。

### Sydney（需 API Key）

```bash
export TFNSW_API_KEY="你的TfNSW_API_Key"
python3 Scripts/import_gtfs.py sydney
```

首次运行下载约 1.7GB 的 GTFS 数据，后续跳过下载。

### 两个城市一起导入

```bash
export TFNSW_API_KEY="你的TfNSW_API_Key"
python3 Scripts/import_gtfs.py all
```

## 运行示例

```
$ python3 Scripts/import_gtfs.py melbourne

🚂 Importing MELBOURNE GTFS Data
[PTV] ⬇️  Downloading static GTFS (~260MB)...
[PTV] ✅ Downloaded: 260.3 MB in 45.2s
[Extract] 📦 Unzipping melbourne_gtfs.zip...
[Extract]   📦 Extracting nested: 1/google_transit.zip
[Extract]   📦 Extracting nested: 2/google_transit.zip
...
[Extract] 📂 Found 8 GTFS file group(s)

[Import] 📋 Processing group: root
  stops: 31,026 total (1.2s)
  routes: 1,077 total (0.1s)
  trips: 400,143 total (3.5s)
  stop_times: 15,570,958 total (85.3s)
  calendar: 7,275 total (0.1s)
  calendar_dates: 120,590 total (0.3s)

==================================================
✅ MELBOURNE import complete!
  stops: 31,026
  routes: 1,077
  trips: 400,143
  stop_times: 15,570,958
  calendar: 7,275
  calendar_dates: 120,590
  Total: 16,131,069 records
  Total time: 114.3s (1.9 min)
```

## 数据存储

所有数据存放在 `~/.traintracker/` 目录下：

```
~/.traintracker/
├── data/
│   ├── melbourne_gtfs.zip   # PTV 原始 GTFS 数据
│   └── sydney_gtfs.zip      # TfNSW 原始 GTFS 数据
└── gtfs.db                  # SQLite 数据库
```

### 数据库 Schema

SQLite 数据库包含以下表：

| 表名 | 说明 | 索引 |
|------|------|------|
| `stops` | 站点信息（名称、经纬度、站台） | stop_name, parent_station, region |
| `routes` | 线路（短名、长名、线路类型） | region, route_type |
| `trips` | 班次（所属线路、服务日、方向） | route_id, service_id |
| `stop_times` | 到站时间（按班次 + 站序） | stop_id, trip_id |
| `calendar` | 服务日历（周几有效） | start/end date |
| `calendar_dates` | 例外日期（节假日等） | service_id, date |
| `meta` | 元数据（最后导入时间等） | key (PK) |

所有表都有 `region` 字段区分城市（`sydney` / `melbourne`），支持多城市数据共存。

### 查询示例

```sql
-- 查找站点
SELECT * FROM stops 
WHERE stop_name LIKE '%Central%' AND region = 'melbourne';

-- 查某站未来发车
SELECT st.arrival_seconds, st.departure_seconds, t.trip_headsign, r.route_short_name
FROM stop_times st
JOIN trips t ON st.trip_id = t.trip_id AND st.region = t.region
JOIN routes r ON t.route_id = r.route_id AND t.region = r.region
WHERE st.stop_id = '20043' 
  AND st.departure_seconds BETWEEN 36000 AND 43200  -- 10:00-12:00
  AND st.region = 'melbourne'
ORDER BY st.departure_seconds
LIMIT 20;
```

## 数据源

| 城市 | 机构 | 下载地址 | 许可证 |
|------|------|----------|--------|
| Sydney | Transport for NSW | [TfNSW Open Data](https://opendata.transport.nsw.gov.au/) | [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Melbourne | Public Transport Victoria | [PTV Open Data](https://opendata.transport.vic.gov.au/) | [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

使用数据时请遵循 CC-BY 4.0 许可证要求，注明数据来源。

## 定时更新

GTFS 数据通常每周更新一次。建议设置 cron job 自动拉取最新数据：

```bash
# 每周日凌晨 2:00 更新 Melbourne + Sydney
# crontab -e
0 2 * * 0 export TFNSW_API_KEY="你的Key" && /path/to/python3 /path/to/Scripts/import_gtfs.py all >> /tmp/gtfs_import.log 2>&1
```

## 技术细节

### 下载策略
- 使用 `curl -L` 处理重定向
- 已有文件且大于 1MB 时自动跳过下载
- Melbourne 无鉴权直链下载；Sydney 通过 HTTP Header 传 API Key

### 解析策略
- PTV 数据是嵌套 ZIP 结构（外层 ZIP → 编号文件夹 → `google_transit.zip`），脚本自动处理嵌套解压
- CSV 自动处理 UTF-8 BOM（`utf-8-sig` 编码）
- 大表（stop_times）采用批量 INSERT + WAL 模式，1550 万行 ~90 秒

### 数据库性能
- WAL 日志模式：读写并发不阻塞
- `cache_size=-64000`：64MB 缓存减少磁盘 I/O
- 每批次 50,000-100,000 条批量插入
- 单事务提交，避免逐行 fsync

## License

MIT License

数据受 [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/) 许可证保护，版权归各机构所有。
