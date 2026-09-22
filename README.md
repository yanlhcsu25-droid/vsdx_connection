# WIA 图解析小程序交付包

本程序从原生 Visio `.vsdx` 图中提取连接事实，并输出：

- 原始连接表：CSV、Markdown；
- 解析校验报告：JSON；
- WIA 格式工作簿：`hierarchy`、`port`、`connect` 和
  `connection_issues` 四个工作表；
- 疑问项：不能从图中可靠确定的信息单独写入 JSON，不猜测填充。
- 语义实体：保留VLM判断过的Shape角色、文本、owner和页面证据，供文档实体融合与追溯。

## 目录

```text
code/                 核心程序
config/               视觉接口配置（不含 API Key）
semantic_context/     文档实体目录格式和当前模拟样例
tests/                自动化测试
examples/             两张可复现实验的 VSDX 与页面 PNG
results/              两张图的最近一次识别结果和调试材料
output/               后续运行生成的新结果（自动创建）
docs/                 识别流程与评测报告
```

## 环境

- Python 3.11 或更高版本；
- 核心程序只使用 Python 标准库，不需要安装第三方依赖；
- 视觉接口需兼容 Chat Completions，并支持图片输入。

API Key 通过环境变量传入，不写入配置文件：

```bash
export VSDX_VISION_API_KEY='你的 API Key'
```

`config/vision.json` 是本次实验使用的非敏感配置，`config/vision.example.json` 是配置示例。

两个解析入口都会根据源码位置确定项目根目录。如果省略命令中的输出目录，程序会在运行时自动创建唯一目录：

```text
output/<输入文件名>_<YYYYMMDD_HHMMSS_microseconds>/
```

视觉解析时如果省略 `--debug-dir`，debug文件也会自动放入这个运行目录的 `debug/` 子目录。同一输入连续运行不会覆盖前一次结果。

## 一键运行

普通用户只需要提供一个 VSDX：

```bash
python3 code/parse_native_vsdx_connections.py /path/to/input.vsdx
```

程序会自动读取 `config/vision.json`，创建页面图片临时目录和时间戳输出目录。当前正式流程不接入文档实体目录，也不会默认加载 `semantic_context/mock_document_entities.json`；只有显式传入 `--entity-catalog` 时才会使用该高级接口。

部署环境必须提供一个能够把VSDX渲染为PNG的渲染器。可以通过项目的 `--render-config` 高级参数或环境变量 `VSDX_RENDER_COMMAND` 配置，命令数组中使用 `{vsdx}` 和 `{output_dir}` 占位符。若未配置，程序会尝试系统中的 `drawio` 和 `soffice`；只有实际生成 `pageN.png` 才会继续。

成功运行后的用户友好文件名为：

```text
<输入文件名>_<时间戳>/
├── connections.csv
├── connections.xlsx
├── validation.json
├── semantic_entities.json
├── questions.json
└── report.md
```

## 开发调试运行 Architecture1 示例

在本目录执行：

```bash
/usr/bin/time -p python3 code/parse_native_vsdx_connections.py \
  examples/architecture1/architecture1_native_right.vsdx
```

## 运行 Wujian100 示例

```bash
/usr/bin/time -p python3 code/parse_native_vsdx_connections.py \
  examples/wujian100/wujian100_reconstructed_fixed.vsdx
```

不要在正式测试时添加 `--semantic-json`；该参数用于回放旧的模型响应，不会重新调用视觉接口。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 当前输出原则和限制

1. XML/VSDX 负责 Shape、Group、Glue、连接端点和图形箭头等连接事实。拓扑先确定，语义只能读取拓扑，不能反向修改拓扑。
2. 每个包含连接器的相关页面固定执行一次VLM语义任务，判断 module、port、owner 和视觉标签角色；空白页、封面页和无连接器的纯说明页跳过。技术重试保持同一任务与prompt，不进行多轮语义修答案。
3. 当前正式流程只有VSDX/XML与页面图片。`semantic_context/mock_document_entities.json` 仅为开发样例，不会自动加载。
4. 模块层次表和顶层端口表只写入有充分证据的数据。无法区分的信息进入 `*_疑问项.json`，因此 `hierarchy` 和 `port` 可能只有表头。
5. Visio箭头只表示图形方向，不自动等价于RTL `input`、`output` 或 `inout`。
6. 最终工作簿始终保持 `hierarchy`、`port`、`connect`、
   `connection_issues` 四个工作表。`connection_issues` 只记录原生
   Begin/End 端点关系不完整的连接器，不推测缺失端的模块，也不输出模型原始回复。
   准确率与方法说明见 `docs/识别流程与双图评测报告.md`。

## 文档实体目录契约

协议实体使用 `canonical_name`、`aliases` 和 `entity_type=protocol`。模块层级还需要提供 `module_name`、`instance_name` 和 `parent_instance_name`。顶层端口需要提供 `entity_type=port`、`is_top_level=true`、`direction`，并可选提供 `width`。

视觉识别出的普通模块端口不会自动升级为顶层端口；只有原生结构无owner，或文档实体明确标记为顶层端口时，才会进入 `port` 工作表。

每次运行还会生成 `*_语义实体.json`。该文件保留所有VLM Shape判断，避免在生成连接表后丢失模块、端口、容器和协议标签信息。
