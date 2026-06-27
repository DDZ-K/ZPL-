ZPL 标签打印工具

运行方式：
方式一：
双击 run.bat

方式二：
打开 PowerShell，执行：
   python "$env:USERPROFILE\Desktop\ZPL标签打印工具\main.py"

打包方式：
双击 build.bat，打包后的 exe 位于 dist\ZPL标签打印工具.exe

依赖：
- Python 3.8+
- PyQt5
- pywin32
- pyinstaller（build.bat 会自动检查并安装）

当前版本说明：
- 已实现 PyQt5 三栏 GUI。
- 支持导入 .zpl / .prn 模板。
- 支持解析 @变量名@ 并生成输入表单。
- 支持“需要替换”复选框，取消勾选后保留原模板变量。
- 支持模板列表持久化、缺失文件提示、模板显示名重命名、从列表移除模板。
- 支持“工具箱”菜单：打印机设置、外观主题、导入模板、导入/导出配置。
- 支持每个模板保存多套变量配置方案。
- 支持右侧 ZPL 文本预览 + Labelary 标签图片预览，图片缓存位于 cache 文件夹。
- 支持实时 ZPL 预览。
- 支持 TCP/IP 与 USB 打印设置，并保存到用户目录 .zpl_printer_settings.json。
- TCP 模式会直接连接设置中的 IP/端口并发送原始 ZPL 数据。
- Windows USB 模式会枚举 USB 打印机端口（如 USB001），并通过 win32print 发送 RAW ZPL。

最终 exe：
dist\ZPL标签打印工具.exe
