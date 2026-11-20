#!/usr/bin/env python3
"""检查测试报告/测试记录的写法，找出“像 AI 写的”地方。

输入二选一：
  --file report.md          本地 Markdown 或纯文本
  --doc <docx token 或 URL>   用 lark-cli 读飞书文档（markdown）

例：
  lint_report_style.py --doc NGsZdOOGdoVUdYxXx0RcfXpknNe
  lint_report_style.py --file 2026-10-29-LBMotion控制器测试报告.md --strict

有错误时退出码为 1；加 --strict 时警告也算失败。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

BANNED_HEADINGS = {
    "修订记录": "不写修订记录/变更记录",
    "变更记录": "不写修订记录/变更记录",
    "修改记录": "不写修订记录/变更记录",
    "版本历史": "不写修订记录/变更记录",
    "下一步": "结尾不列“下一步”，问题写进结论或小结",
    "后续计划": "结尾不列“下一步”，问题写进结论或小结",
    "后续工作": "结尾不列“下一步”，问题写进结论或小结",
    "后续安排": "结尾不列“下一步”，问题写进结论或小结",
    "待办事项": "结尾不列“下一步”，问题写进结论或小结",
}
HEDGE_PATTERNS = [
    r"可能", r"大约", r"(?<![预节制契隐大])约(?=\s?[\d一二两三四五六七八九十半百千])", r"大概", r"似乎",
    r"或许", r"仅供参考", r"[需待]研发确认", r"有待进一步", r"初步判断", r"疑似", r"应该是",
]
AI_PHRASES = [
    "综上所述", "值得注意的是", "总的来说", "总而言之", "不难看出", "显而易见", "本报告旨在",
    "本文将", "让我们", "需要指出的是", "至关重要", "深入探讨", "全面提升", "赋能", "无缝",
]
APPENDIX_REF = re.compile(r"(详见|参见|见)\s*(附录|附件)")
# 主观判断（11-09 Cyres / Billow 定）：报告里绝对不能出现，必须用数据说话。
# 比较控制器或方法也要控制变量，用任务成功率、每次耗时、CPI 等指标衡量。
SUBJECTIVE = re.compile(
    r"(?<!本体)感觉|觉得|感受|主观|口头反馈|操作员反馈|手感|体感上|印象中?|好像|似乎|貌似|看起来|看上去|"
    r"直观上|我认为|个人认为|据反馈")
# 好坏、程度这类评价词：同一句里没有数字就算主观判断
QUALITY = re.compile(
    r"(?:综合)?表现(?:最好|更好|较好|较差|最差|一般|良好|不错|优秀)|综合表现|效果(?:最好|更好|较好|较差|一般|不错|明显)|"
    r"更顺|更流畅|更稳定?|更自然|更跟手|更灵敏|不太稳|比较稳|很稳|很好|很差|较好|较差|良好|优秀|不错|"
    r"顺畅|流畅|自然|跟手|丝滑|明显(?:好转|改善|变差|更)|大幅|略有|稍微|有所(?:改善|提升|下降)")
DIGIT = re.compile(r"\d")
SEG_SPLIT = re.compile(r"[。！？；\n|]|</t[dh]>")
THIRD_PERSON = re.compile(r"用户(?!身高)|测试人员|助手|本机|AI\s*生成")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2705\u274C]")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
TAG = re.compile(r"<[^>]+>")
SENT_SPLIT = re.compile(r"[。！？；\n]")

HEDGE_DENSITY_MAX = 3.0   # 每千字
WO_DENSITY_MAX = 5.0      # 每千字
BOLD_MAX = 15
SENTENCE_MAX = 120
OPENING_LINES = 15
OPENING_PAREN_MAX = 40


def lark_cli():
    for cand in (os.environ.get("LARK_CLI"), shutil.which("lark-cli"),
                 os.path.expanduser("~/.local/bin/lark-cli")):
        if cand and os.path.exists(cand):
            return cand
    raise SystemExit("找不到 lark-cli，用 LARK_CLI 环境变量指定路径")


def fetch_doc(doc):
    res = subprocess.run([lark_cli(), "docs", "+fetch", "--doc", doc, "--doc-format", "markdown"],
                         capture_output=True, text=True, cwd="/tmp")
    if res.returncode != 0:
        raise SystemExit(f"lark-cli 读取失败：{res.stderr.strip() or res.stdout.strip()}")
    obj = json.loads(res.stdout)
    data = obj.get("data", {})
    content = (data.get("document") or {}).get("content") or data.get("content")
    if not content:
        raise SystemExit("没有读到文档内容")
    return content


def plain(line):
    return TAG.sub("", line).replace("**", "").strip()


def lint(text):
    items = []
    lines = text.splitlines()
    body_chars = 0
    hedge_total = 0
    wo_total = 0
    bold_total = 0
    content_seen = 0
    in_code = False

    for no, raw in enumerate(lines, 1):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        line = plain(raw)
        if not line:
            continue
        body_chars += len(line)
        bold_total += raw.count("**") // 2

        subj = sorted(set(SUBJECTIVE.findall(line)))
        if subj:
            items.append((no, "E", f"主观判断：{'、'.join(subj)}，改成数据（次数、成功率、耗时、角度、误差）—— {line[:40]}"))
        for seg in SEG_SPLIT.split(raw):
            seg = plain(seg)
            q = sorted(set(QUALITY.findall(seg)))
            if q and not DIGIT.search(seg):
                items.append((no, "E", f"评价没有数据：{'、'.join(q)}，同一句写出依据的数 —— {seg[:40]}"))

        m = HEADING.match(raw)
        if m:
            title = plain(m.group(1))
            for key, why in BANNED_HEADINGS.items():
                if key in title:
                    items.append((no, "E", f"标题“{title}”：{why}"))
            continue

        content_seen += 1
        if content_seen <= OPENING_LINES:
            for paren in re.findall(r"（([^（）]*)）", line):
                if len(paren) > OPENING_PAREN_MAX:
                    items.append((no, "E", f"开头有一长串括号说明（{len(paren)} 字），改成几条平实要点：（{paren[:30]}…）"))
            if line.startswith("摘要") and "（" in line:
                items.append((no, "E", "开头写了“摘要（…）”，改成几条平实要点"))

        hits = []
        for pat in HEDGE_PATTERNS:
            for hm in re.finditer(pat, line):
                hits.append(hm.group(0))
        if hits:
            hedge_total += len(hits)
            items.append((no, "W", f"保留措辞：{'、'.join(hits)} —— {line[:40]}"))

        phrases = [p for p in AI_PHRASES if p in line]
        if phrases:
            items.append((no, "W", f"套话：{'、'.join(phrases)}"))
        if APPENDIX_REF.search(line):
            items.append((no, "W", "写了“见附录/附件”，证据直接贴在结论下面"))
        tp = THIRD_PERSON.findall(line)
        if tp:
            items.append((no, "W", f"第三方视角用词：{'、'.join(tp)}（站在 Cyres 角度写）"))
        if EMOJI.search(line):
            items.append((no, "W", "有 emoji"))
        if "！" in line or "!" in re.sub(r"!\[", "", line):
            items.append((no, "W", "有感叹号"))
        wo_total += line.count("我")
        is_table = raw.lstrip().startswith(("<table", "|")) or "<td" in raw or "<th" in raw
        for sent in ([] if is_table else SENT_SPLIT.split(line)):
            if len(sent) > SENTENCE_MAX:
                items.append((no, "W", f"句子太长（{len(sent)} 字），拆开写：{sent[:30]}…"))

    if "首先" in text and "其次" in text:
        items.append((0, "W", "用了“首先……其次……”的结构"))
    k = max(body_chars, 1) / 1000
    if hedge_total / k > HEDGE_DENSITY_MAX:
        items.append((0, "W", f"保留措辞偏多：{hedge_total} 处，每千字 {hedge_total / k:.1f}（上限 {HEDGE_DENSITY_MAX}）"))
    if wo_total / k > WO_DENSITY_MAX:
        items.append((0, "W", f"“我”用得偏多：{wo_total} 处，每千字 {wo_total / k:.1f}"))
    if bold_total > BOLD_MAX:
        items.append((0, "W", f"加粗 {bold_total} 处，太多"))
    return items, body_chars, hedge_total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--doc")
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = fetch_doc(args.doc)

    items, chars, hedges = lint(text)
    if not args.quiet:
        for no, lv, msg in sorted(items, key=lambda x: (x[0], x[1])):
            where = f"行 {no}" if no else "整体"
            print(f"[{lv}] {where}: {msg}")
        if items:
            print()
    errs = sum(1 for i in items if i[1] == "E")
    warns = sum(1 for i in items if i[1] == "W")
    print(f"正文 {chars} 字：错误 {errs}，警告 {warns}，保留措辞 {hedges} 处")
    sys.exit(1 if errs or (args.strict and warns) else 0)


if __name__ == "__main__":
    main()
