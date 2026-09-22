# 视觉模型配置

复制示例配置：

```bash
cp vision.example.json vision.json
```

然后修改 `vision.json` 中的 `url` 和 `model`。API Key 不写进文件，使用
`api_key_env` 指定的环境变量传入：

```bash
export VSDX_VISION_API_KEY='你的 API Key'
```

`vision.json` 供 `parse_native_vsdx_connections.py --vision-config` 使用。
当前 HTTP 适配器要求接口兼容 Chat Completions，并支持图片输入。`semantic_retry_attempts`
控制分类漏标后的定向补问次数，默认值为 2；达到次数后仍有遗漏会标记为 `incomplete`。

普通用户无需显式传入该参数；程序会自动读取项目内的 `vision.json`。部署环境只需设置：

```bash
export VSDX_VISION_API_KEY='你的 API Key'
```

如果部署环境需要指定VSDX渲染命令，可设置 `VSDX_RENDER_COMMAND` 为JSON字符串数组，例如：

```bash
export VSDX_RENDER_COMMAND='["/path/to/renderer", "--input", "{vsdx}", "--output-dir", "{output_dir}"]'
```
