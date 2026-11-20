"""给 README 截图：演练模式下打开各页，按区域裁图。

用法：先在 backend/ 下用演练数据起服务（端口 8799），再 python docs/shoot_readme.py http://127.0.0.1:8799 docs/readme
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1], Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1.25)

    def go(h):
        page.goto(f"{BASE}/#{h}")
        page.wait_for_timeout(2500)

    go("wbt")
    page.screenshot(path=str(OUT / "1_全身回放.png"))
    page.locator("article").first.screenshot(path=str(OUT / "2_回放结果.png"))
    go("lbm")
    page.locator("section").nth(1).screenshot(path=str(OUT / "3_LBMotion一键.png"))
    go("gripper")
    page.screenshot(path=str(OUT / "4_夹爪.png"))
    page.locator("header button:has-text('切换状态')").click()
    page.wait_for_timeout(800)
    page.screenshot(path=str(OUT / "5_切换状态.png"), clip={"x": 880, "y": 0, "width": 560, "height": 420})
    b.close()
print("done")
