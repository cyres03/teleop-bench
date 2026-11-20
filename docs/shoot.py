"""给使用说明截图：无头 Chrome 打开遥操测试台（演练服务器），按元素裁图。

用法：python docs/shoot.py http://127.0.0.1:8799 docs/img
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1], Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)


def section(page, text):
    return page.locator("section").filter(has_text=text).last


with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5)
    page.goto(f"{BASE}/#latency")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / "00_界面.png"))
    shots = [
        ("01_闪烁页.png", lambda: section(page, "打开闪烁页")),
        ("02_今日矩阵.png", lambda: page.locator("div.rounded-md").filter(has_text="链路与编码").first),
        ("03_选相机.png", lambda: section(page, "选相机并看一眼画面")),
        ("04_校准.png", lambda: section(page, "两台相机都对着屏幕录")),
        ("05_测量.png", lambda: section(page, "B 放回 Pico 镜片前")),
        ("06_历次结果.png", lambda: page.locator("table").last),
    ]
    for name, loc in shots:
        try:
            el = loc()
            el.scroll_into_view_if_needed()
            el.screenshot(path=str(OUT / name))
            print("saved", name)
        except Exception as e:
            print("missing", name, str(e).splitlines()[0])
    try:
        page.get_by_role("button", name="生成测试记录").first.click()
        page.get_by_text("写作规范检查：错误").first.wait_for(timeout=180_000)
        page.wait_for_timeout(800)
        el = section(page, "生成测试记录")
        el.scroll_into_view_if_needed()
        el.screenshot(path=str(OUT / "07_生成记录.png"))
        print("saved 07_生成记录.png")
    except Exception as e:
        print("missing 07", str(e).splitlines()[0])
    page.goto(f"{BASE}/#ledger")
    page.wait_for_timeout(2000)
    page.screenshot(path=str(OUT / "08_指标台账.png"))
    print("saved 08_指标台账.png")
    b.close()
