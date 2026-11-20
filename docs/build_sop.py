"""生成《遥操测试台使用 SOP》飞书文档（DocxXML）和源码包。

用法：python docs/build_sop.py [输出目录]；默认 docs/sop_out。
发布：在输出目录里 lark-cli docs +create --parent-position my_library --content @sop.xml
"""
import shutil
import sys
import zipfile
from html import escape as E
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "sop_out"
TITLE = "遥操测试台使用 SOP（视频延迟测试）"
SKIP = {"node_modules", "dist", "__pycache__", ".pytest_cache", "sop_out", ".vite", ".tmp"}


def p(t: str) -> str:
    return f"<p>{t}</p>"


def code(t: str) -> str:
    return f"<code>{E(t)}</code>"


def ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


def ol(items: list[str]) -> str:
    return "<ol>" + "".join(f"<li>{x}</li>" for x in items) + "</ol>"


def table(head: list[str], rows: list[list[str]]) -> str:
    cols = "".join("<col/>" for _ in head)
    h = "".join(f'<th vertical-align="top">{p(E(x))}</th>' for x in head)
    b = "".join("<tr>" + "".join(f'<td vertical-align="top">{p(c)}</td>' for c in r) + "</tr>" for r in rows)
    return f"<table><colgroup>{cols}</colgroup><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def img(name: str, cap: str) -> str:
    return f'<img path="@./img/{name}" caption="{E(cap)}"/>'


def card(name: str) -> str:
    return f'<figure view-type="Card"><source path="@./{name}"/></figure>'


def make_zip(dst: Path) -> None:
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(ROOT.rglob("*")):
            rel = f.relative_to(ROOT)
            if f.is_dir() or SKIP & set(rel.parts) or f.suffix in {".pyc", ".zip"}:
                continue
            z.write(f, Path("teleop-bench") / rel)


def build() -> str:
    x = [f"<title>{E(TITLE)}</title>"]
    x.append("<h1>要点</h1>")
    x.append(ul([
        "遥操测试台是一个桌面软件，用来测 Oli 遥操的端到端视频延迟，也带指标台账、模式切换压测、长测看护。",
        "延迟测法：外接屏上的闪烁块加两台 USB 双目相机，A 拍屏幕，B 拍 Pico 镜片，B 里每次黑白翻转减去 A 里同一次翻转的时间就是延迟。",
        "结果看均值和 P5–P95。近场目标 ≤100 ms，远程目标 ≤150 ms（提测单 2.4.5）。",
        "一套流程：打开闪烁页 → 选相机 → 校准 → 测量 → 看矩阵 → 生成测试记录 → 发到飞书。每格至少测 2 次。",
        "今天要测的矩阵：近场 H264、H265、JPEG，远程阿里云 H264、腾讯云 H264。",
    ]))
    x.append(img("00_界面.png", "主界面：顶部是当前机器人，上方是今日矩阵"))

    x.append("<h1>准备</h1>")
    x.append("<h2>硬件</h2>")
    x.append(table(["物品", "要求"], [
        ["电脑", "Ubuntu 22.04，接一块外接屏放闪烁页"],
        ["相机 A、B", "两台 USB 双目相机（2560×720）。<b>分开插在两个不同的 USB 口上</b>，不要插同一个 USB 2.0 HUB，否则第二台打不开"],
        ["Pico", "已装遥操 App，能连上要测的机器人"],
        ["机器人", "Oli，电脑能连上它的热点（主控 10.192.1.2）"],
        ["支架", "A 对准外接屏；B 贴着 Pico 左眼镜片，能拍到画面里的闪烁块"],
    ]))
    x.append("<h2>账号和密钥</h2>")
    x.append(ul([
        f"读取、切换近场编码要 SSH 到主控，默认用密钥 {code('~/.ssh/oli_robot_manager_ed25519')}（和 oli-robot-manager 用的同一把），用户 limx。主机密钥要在电脑的 {code('~/.ssh/known_hosts')} 里有记录，第一次可先在终端 ssh 一次。",
        "发到飞书要先装好 lark-cli 并登录（终端执行 lark-cli auth status 能看到自己的账号）。",
        "不用这两项也能测延迟和生成本地记录，只是不能在软件里切编码、不能一键发布。",
    ]))

    x.append("<h1>安装</h1>")
    x.append(p("源码包："))
    x.append(card("teleop-bench_src.zip"))
    x.append(ol([
        "先装 Miniforge（mamba）：https://github.com/conda-forge/miniforge ，装在默认位置 ~/miniforge3。",
        f"解压源码包到 {code('~/ai/teleop-bench')}。",
        f"终端执行 {code('bash ~/ai/teleop-bench/install.sh')}。脚本会建 conda 环境 teleop-bench，装依赖，构建界面，加应用菜单和桌面图标，最后自检 25 条测试。",
        "最后显示“25 passed”和“装好了”即可。整个过程大约 5–10 分钟，需要联网。",
    ]))
    x.append(p("启动：桌面或应用菜单里点“遥操测试台”。顶部条显示 SN、固件、状态、电量就说明认到了机器人；没认到就连上热点后点“重新识别”。勾选“演练模式”可以不连机器人试用界面，演练结果不写台账。"))

    x.append("<h1>视频延迟测试步骤</h1>")
    x.append("<h2>1. 看今日矩阵</h2>")
    x.append(p("页面最上面是要测的 5 格。圆点是次数（实心为已测），横条是均值，细线是 P5–P95，橙色竖线是目标。点一行，下面测量用的链路和编码会切到这一格。"))
    x.append(img("02_今日矩阵.png", "今日矩阵：横条为均值，细线为 P5–P95，橙线为目标"))
    x.append("<h2>2. 打开闪烁页</h2>")
    x.append(p("点“在外接屏打开闪烁页”，闪烁页会在外接屏全屏打开，左右两半随机黑白翻转。机器人头部相机对准它。Alt+F4 退出。"))
    x.append(img("01_闪烁页.png", "第 1 步：打开闪烁页"))
    x.append("<h2>3. 选相机并截图</h2>")
    x.append(p("A 选拍屏幕的那台，B 选拍 Pico 的那台，各点一次截图，确认两路画面里都能看到闪烁块。列表里没有相机时，插好后点“刷新相机列表”。"))
    x.append(img("03_选相机.png", "第 2 步：选相机，各截一张确认画面"))
    x.append("<h2>4. 校准</h2>")
    x.append(p("把 A、B 都对着外接屏，点“录 30 s 校准”。得到的是两台相机自身的时间差，后面测量会自动扣掉。正常在 ±5 ms 以内；换了相机或 USB 口要重新校准。"))
    x.append(img("04_校准.png", "第 3 步：校准，结果显示在按钮右边"))
    x.append("<h2>5. 确认机器人编码，测量</h2>")
    x.append(ol([
        "B 放回 Pico 镜片前，Pico 进入遥操，画面稳定。",
        "近场：点“读取”看机器人当前的近场编码。和要测的不一致时点“切到 X 并重启视频服务”，确认后软件会备份原配置、改编码、重启视频服务、读回核对。Pico 要重新连接，并在 Pico 上确认画面正常（头显不支持时会自动退回 JPEG）。",
        "远程：阿里云和腾讯云的切换要换服务端或配置，目前在主控上手动做，软件里只选链路做记录。",
        "名称填一个好认的，例如 jpeg1、ali1。软件会自动加上时分秒，同名不会覆盖。",
        "点“开始测量”，默认录 30 s，再分析约 1 分钟。结果显示在下方：均值、P5–P95、配上的翻转次数。",
        "近场测完点“还原原配置”，用当天最早的备份还原。",
    ]))
    x.append(img("05_测量.png", "第 4 步：读取编码，选链路和编码，开始测量"))
    x.append("<h2>6. 检查历次结果</h2>")
    x.append(p("每次结果自动进“历次结果”和上面的矩阵。判断一次是否有效：配对数应接近翻转数（至少一半），P5–P95 不应异常宽。无效的一次点垃圾桶删掉，不进对比和报告。"))
    x.append(img("06_历次结果.png", "历次结果：配对 / 翻转接近 1:1 才有效"))
    x.append("<h2>7. 生成测试记录并发布</h2>")
    x.append(ol([
        "填依据文档（默认是延迟测试计划）和补充说明（场地、Pico 版本、远程网络环境等）。",
        "点“生成测试记录”，约 1–3 分钟。软件按《专项测试记录模板》生成：要点、测试准备、结果表、对比图、每次的分布图和 A|B 画面、10 s A|B 并排视频、脚本原件、结论和问题表。",
        "生成后自动做写作规范检查，错误必须为 0 才能发布。展开“看正文文字”通读一遍。",
        "确认无误点“发到飞书”，文档建在自己的“我的文档库”里，再移到需要的目录。",
    ]))
    x.append(img("07_生成记录.png", "生成测试记录：检查错误为 0 后发到飞书"))

    x.append("<h1>常见问题</h1>")
    x.append(table(["现象", "原因", "处理"], [
        ["分析没有得出统计结果，提示相机 B 没录到", "相机录像中途 USB 掉线（系统日志里是 error -71），或设备号变了",
         "重新插相机，最好换一个 USB 口或换线；点“刷新相机列表”，重新选相机，重新校准后再测"],
        ["第二台相机打不开，提示 No space left on device", "两台相机插在同一个 USB 2.0 HUB 上，带宽不够", "分开插到电脑不同的 USB 口"],
        ["配对数不到翻转数的一半", "B 没对准镜片里的闪烁块，或画面卡顿、黑屏", "调整 B 的位置，确认 Pico 画面流畅，删掉这次重测"],
        ["校准值超出 ±5 ms", "相机帧率不稳或拍屏幕时曝光过度", "检查两台相机都是 2560×720 30 fps，调暗屏幕或环境后重新校准"],
        ["顶部显示没有识别到机器人", "电脑网卡自动切回了别的网络", f"重新连机器人热点，例如 {code('nmcli con up 热点名 ifname 网卡名')}，再点“重新识别”"],
        ["切到 H264 / H265 后 Pico 画面实际是 JPEG", "头显不支持该编码，自动退回 JPEG", "以 Pico 上实际编码为准记录，必要时升级 Pico App"],
        ["读取编码报 SSH 失败", "没有密钥，或主机密钥没记录", "按“账号和密钥”配置，先在终端 ssh 一次主控"],
        ["发到飞书失败", "lark-cli 没登录或登录过期", "终端执行 lark-cli auth status 检查，过期就重新登录"],
    ]))

    x.append("<h1>数据位置</h1>")
    x.append(ul([
        f"所有数据在 {code('~/.teleop-bench')}：台账数据库、录像、分析结果、生成的测试记录、配置 config.json。",
        f"每次测量的录像和结果在 {code('~/.teleop-bench/latency/日期/')} 下，文件名是“名称_时分秒”；生成的测试记录在 {code('~/.teleop-bench/reports/')}。",
        f"机器人编码配置的备份在主控 {code('~/Cyres/backup_video_日期/')}。",
        "换电脑时整个目录拷过去即可。",
    ]))

    x.append("<h1>其他功能</h1>")
    x.append(table(["页面", "用途"], [
        ["指标台账", "按功能场景记录 CPI / HPI，0 次问题记成“总数+”，可一键复制成飞书文本"],
        ["模式切换压测", "坐下 ↔ 起身、躺下 ↔ 起身、Walk ↔ 动作库循环压测，结束按 CPI 写进台账。真机要勾选“已确认现场”"],
        ["长测看护", "长测计时，自动检测异常停机和疑似摔倒，人工逐条确认，结束写 HPF 和 HPI"],
        ["使用说明", "软件内置的完整说明"],
    ]))
    x.append(img("08_指标台账.png", "指标台账"))
    x.append(p("改机器人配置、重启服务、下发运动指令前软件都会要求确认。软件不保存密码。问题和建议找罗嘉轩（Cyres）。"))
    return "\n".join(x)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "img").mkdir(parents=True)
    for f in (HERE / "img").glob("*.png"):
        shutil.copy(f, OUT / "img" / f.name)
    make_zip(OUT / "teleop-bench_src.zip")
    (OUT / "sop.xml").write_text(build(), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
