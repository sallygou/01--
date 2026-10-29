#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公共仓协作规则校验器（零依赖）

判定依据：01-早鸟票公共仓/02-协作契约/YAML字段规范-v1.md
本脚本只做**结构校验**（字段、取值、编号、目录、发布红线），
不判断业务方案是否正确（如接口设计是否合理、退回是否有理）。

用法：
    python validate.py 文件1.md 文件2.md ...        # 校验指定文件
    python validate.py --all                        # 校验三个仓库全部 Markdown
    python validate.py --repo-root <公共仓路径> 文件.md
退出码：0 无错误；1 存在错误
"""
from __future__ import annotations

import os
import re
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------- 规范常量 ----------------
DOC_TYPES = ["协同单", "回执单", "退回单", "交付物", "验收结论", "动态知识", "静态知识"]
VOUCHER_TYPES = ["协同单", "回执单", "退回单"]
STATUS_VALUES = ["草稿", "已发出", "已接收", "进行中", "待验收", "已完成",
                 "已退回", "已归档", "已作废"]
AUDIT_CONCLUSIONS = ["通过", "退回"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VOUCHER_ID_RE = re.compile(r"^(协同单|回执单|退回单)-\d{8}-\d{3}$")
DOC_ID_RE = re.compile(r"^[A-Za-z\-]+-?\d{0,8}-?\d{0,3}$")

# 免 YAML 头要求的文件（说明类、模板类、任务包六件）
EXEMPT_NAMES = {
    "README.md", "AGENTS.md", "索引-预留.md", "任务看板.md",
}
EXEMPT_SUFFIX = ("-模板.md",)
EXEMPT_TASKPARTS = ("01-任务说明.md", "02-输入清单.md", "03-执行计划.md",
                    "04-产出清单.md", "05-协作记录.md", "06-沉淀归档.md")

REL_TASK_KEYS = ["任务包", "任务编号", "来源任务包"]
REL_FILE_KEYS = ["关联文件", "交付物", "关联单据"]


class Issue:
    def __init__(self, level, path, line, rule, msg, fix):
        self.level = level          # ERROR / WARN
        self.path = path
        self.line = line
        self.rule = rule            # 规则编号 R1..R9
        self.msg = msg
        self.fix = fix

    def text(self):
        loc = "%s:%s" % (self.path, self.line if self.line else "-")
        return "  [%s] %s [%s] %s\n        修改建议：%s" % (
            self.level, loc, self.rule, self.msg, self.fix)


# ---------------- 轻量 YAML 解析（无第三方依赖） ----------------
def parse_front_matter(path):
    """返回 (data: dict, lines_map: dict, fm_end_line: int|None, errs: list)"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, {}, None, []       # 无 YAML 头
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, {}, None, ["YAML 头缺少结束分隔符 '---'"]

    data, lmap, errs = {}, {}, []
    i = 1
    while i < end:
        raw = lines[i]
        line_no = i + 1
        if raw.strip() == "" or raw.strip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([^\s:#][^:]*):\s*(.*)$", raw)
        if not m:
            if raw.startswith("  -") or raw.startswith("- "):
                errs.append("第 %d 行列表项缺少上层字段：%s" % (i + 1, raw.strip()))
            elif raw.startswith("\t"):
                errs.append("第 %d 行使用了 Tab 缩进（YAML 不允许）" % (line_no))
            else:
                errs.append("第 %d 行无法解析：%s" % (line_no, raw.strip()))
            i += 1
            continue
        key = m.group(1).strip()
        val = strip_comment(m.group(2)).strip()
        if key in data:
            errs.append("第 %d 行重复字段：%s" % (line_no, key))
        # 收列表项
        if val == "":
            items, j = [], i + 1
            while j < end and re.match(r"^\s*-\s+", lines[j]):
                items.append(strip_comment(lines[j].strip()[1:]).strip())
                j += 1
            data[key] = items
            lmap[key] = line_no
            i = j
            continue
        data[key] = val
        lmap[key] = line_no
        i += 1
    return data, lmap, end, errs


def strip_comment(v):
    out, quote = [], False
    prev = ""
    for ch in v:
        if ch in "\"'":
            quote = not quote
        if not quote and ch == "#" and prev in (" ", ""):
            break
        out.append(ch)
        prev = ch
    return "".join(out).strip()


# ---------------- 环境定位 ----------------
def find_public_root(path):
    """定位公共仓根目录（成员名册所在），向上/同级查找。"""
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(6):
        cand = os.path.join(d, "01-早鸟票公共仓")
        if os.path.isfile(os.path.join(d, "01-成员名册", "成员名册-v1.md")):
            return d
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def load_roster(public_root):
    """从成员名册表格读取成员姓名。"""
    if not public_root:
        return []
    p = os.path.join(public_root, "01-成员名册", "成员名册-v1.md")
    if not os.path.isfile(p):
        return []
    names = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells:
                continue
            first = cells[0]
            if first in ("成员",) or set(first) <= set("-: "):
                continue
            if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z]{2,8}", first):
                names.append(first)
    return names


def scan_index(public_root):
    """扫描三个仓库：已知文件路径集合、任务包目录名集合、编号->路径、审核记录文本。"""
    files, packages, ids = [], set(), {}
    audit_text = ""
    if not public_root:
        return files, packages, ids, audit_text
    workspace = os.path.dirname(public_root)
    for repo in sorted(os.listdir(workspace)):
        repo_path = os.path.join(workspace, repo)
        if not os.path.isdir(repo_path) or repo.startswith("."):
            continue
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            continue
        for root, dirs, fs in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for d in list(dirs):
                if re.match(r"^(ZRT|WXM)-\d{8}-\d{2}", d):
                    packages.add(d)
            for fn in fs:
                full = os.path.join(root, fn)
                files.append(full)
                if fn.endswith(".md"):
                    data, _, _, _ = parse_front_matter(full)
                    if data:
                        for k in ("单据编号", "文档编号"):
                            if data.get(k):
                                ids.setdefault(data[k], []).append(full)
                if fn.startswith("审核记录-") and fn.endswith(".md"):
                    try:
                        with open(full, "r", encoding="utf-8") as fh:
                            audit_text += fh.read() + "\n"
                    except Exception:
                        pass
    return files, packages, ids, audit_text


def rel_display(path, public_root):
    try:
        ws = os.path.dirname(public_root)
        return os.path.relpath(path, ws).replace("\\", "/")
    except Exception:
        return path


# ---------------- 目录权限规则 ----------------
def check_location(ftype, rel, issues, path, lmap):
    """R8 文件所在目录是否符合协作规则"""
    if not ftype:
        return
    if ftype in VOUCHER_TYPES:
        ok = ("/04-交换区/" in "/" + rel or "/01-输入/" in "/" + rel)
        if not ok:
            issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R8",
                                "单据类文件不允许放在此目录：%s" % rel,
                                "移入公共仓 `04-交换区/`（个人仓可放 `01-输入/` 留痕副本）"))
    elif ftype in ("交付物", "验收结论"):
        ok = ("/03-生效区/" in "/" + rel or "动态知识/待审核/" in rel)
        if not ok:
            issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R8",
                                "交付物/验收结论不允许放在此目录：%s" % rel,
                                "移入个人仓 `03-生效区/` 或公共仓 `03-知识区/动态知识/待审核/`"))
    elif ftype == "动态知识":
        if not re.search(r"03-知识区/动态知识/(待审核|current|history)/", rel):
            issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R8",
                                "动态知识不允许放在此目录：%s" % rel,
                                "移入公共仓 `03-知识区/动态知识/{待审核,current,history}/`"))
    elif ftype == "静态知识":
        if "03-知识区/静态知识/" not in rel:
            issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R8",
                                "静态知识不允许放在此目录：%s" % rel,
                                "移入公共仓 `03-知识区/静态知识/`"))


def check_publish(path, rel, data, lmap, issues, roster, audit_text):
    """R9 未经审核发布到正式知识区"""
    published = re.search(r"03-知识区/动态知识/(current|history)/", rel)
    if not published:
        if "03-知识区/动态知识/待审核/" in rel and data.get("审核结论") == "通过":
            issues.append(Issue("WARN", path, lmap.get("审核结论", 1), "R9",
                                "位于待审核区却标记为审核通过",
                                "若已完成审核，移入 `current/` 正式发布"))
        return
    author = data.get("作者", "")
    auditor = data.get("审核人", "")
    concl = data.get("审核结论", "")
    doc_id = data.get("文档编号", "")
    if not auditor:
        issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R9",
                            "进入正式知识区但缺少 `审核人`（未经审核发布）",
                            "先移入 `待审核/`，由非作者成员审核后再移入 `current/`"))
    if concl not in AUDIT_CONCLUSIONS:
        issues.append(Issue("ERROR", path, lmap.get("审核结论", lmap.get("类型", 1)), "R9",
                            "`审核结论` 非法或缺失：%r（应为 通过/退回）" % concl,
                            "补写 `审核结论: 通过`"))
    elif concl != "通过":
        issues.append(Issue("ERROR", path, lmap.get("审核结论", 1), "R9",
                            "审核结论为 %s，不得发布到正式知识区" % concl,
                            "退回作者修改，待通过后重新入库"))
    if auditor and author and auditor == author:
        issues.append(Issue("ERROR", path, lmap.get("审核人", 1), "R9",
                            "审核人与作者相同，不满足交叉审核",
                            "改由另一名成员审核"))
    if doc_id and audit_text and doc_id not in audit_text:
        issues.append(Issue("ERROR", path, lmap.get("文档编号", 1), "R9",
                            "`00-推进管理/审核记录-*.md` 中查不到编号 %s 的审核记录" % doc_id,
                            "先在公共仓补写审核记录，再发布"))


def check_links(data, lmap, rel, issues, path, files, packages, public_root, rel_root):
    """R7 关联任务与文件有效性"""
    task_vals = [data.get(k, "") for k in REL_TASK_KEYS if data.get(k)]
    task_vals = [v for v in task_vals if v]
    if not task_vals:
        issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R7",
                            "缺少关联任务字段（任务包/任务编号/来源任务包）",
                            "补写 `任务包: <任务包编号>`"))
    else:
        for v in task_vals:
            for token in re.split(r"[\s/、,]+", str(v)):
                if not token:
                    continue
                exists = any(token in p for p in packages)
                if not exists:
                    issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R7",
                                        "关联任务 %s 不存在（未在任何任务包目录中检索到）" % token,
                                        "确认任务包编号是否已创建"))
    for key in REL_FILE_KEYS:
        raw = data.get(key)
        if not raw:
            continue
        vals = raw if isinstance(raw, list) else [raw]
        for token in vals:
            if key == "关联单据":
                continue
            cand = str(token).strip().strip("`")
            found = False
            for base in (rel_root, os.path.dirname(public_root) if public_root else None):
                if not base:
                    continue
                if os.path.exists(os.path.join(base, cand.replace("/", os.sep))):
                    found = True
                    break
            if not found:
                issues.append(Issue("ERROR", path, lmap.get(key, 1), "R7",
                                    "`%s` 指向的路径不存在：%s" % (key, cand),
                                    "修正路径，或确认文件已被移动/归档"))


def check_ref_singleolders(data, lmap, issues, path, ids, files):
    """回执单/退回单关联的原协同单必须存在"""
    key = "回复的协同单" if data.get("类型") == "回执单" else "针对的协同单"
    target = data.get(key)
    if data.get("类型") in ("回执单", "退回单"):
        if not target:
            issues.append(Issue("ERROR", path, lmap.get("类型", 1), "R7",
                                "缺少 `%s`" % key,
                                "填写被回复的协同单编号"))
        elif str(target) not in ids:
            issues.append(Issue("ERROR", path, lmap.get(key, 1), "R7",
                                "%s 指向的 %s 不存在" % (key, target),
                                "确认协同单编号是否已创建"))


# ---------------- 主校验 ----------------
def validate_file(path, ctx):
    issues = []
    public_root = ctx["public_root"]
    rel = rel_display(path, public_root) if public_root else os.path.basename(path)
    rel_root = os.path.dirname(public_root) if public_root else os.getcwd()

    fname = os.path.basename(path)
    exempt = (fname in EXEMPT_NAMES or fname.endswith(EXEMPT_SUFFIX)
              or fname in EXEMPT_TASKPARTS or fname.startswith("推进计划"))
    controlled_dir = any(x in "/" + rel for x in ("/04-交换区/", "/03-知识区/")) or \
        "/03-生效区/" in "/" + rel

    data, lmap, fm_end, perrs = parse_front_matter(path)

    if data is None:
        if perrs:
            for e in perrs:
                issues.append(Issue("ERROR", rel, 1, "R1", "YAML 格式错误：%s" % e,
                                    "补齐 YAML 头起止的 `---` 分隔符"))
        elif controlled_dir and not exempt:
            issues.append(Issue("ERROR", rel, 1, "R1",
                                "受控目录下缺少 YAML 结构化头",
                                "按 `YAML字段规范-v1.md` 在文件顶部补 YAML 头"))
        else:
            return issues, "SKIP"
        return issues, "ERROR"

    for e in perrs:
        issues.append(Issue("ERROR", rel, 1, "R1", "YAML 解析问题：%s" % e,
                            "检查缩进、重复字段与分隔符"))

    ftype = data.get("类型", "")

    # 模板文件：只校验 YAML 格式与类型合法性，占位值不参与必填/引用/目录判定
    if fname.endswith("-模板.md"):
        if not ftype:
            issues.append(Issue("ERROR", rel, 1, "R2", "模板缺少 `类型` 字段",
                                "补写 `类型: <单据类型>`"))
        elif ftype not in DOC_TYPES:
            issues.append(Issue("ERROR", rel, lmap.get("类型", 1), "R3",
                                "模板的文档类型非法：%r" % ftype,
                                "改为 %s 之一" % "/".join(DOC_TYPES)))
        lvl = "ERROR" if any(i.level == "ERROR" for i in issues) else "OK"
        return issues, lvl

    # R3 类型是否规范
    if not ftype:
        issues.append(Issue("ERROR", rel, 1, "R3", "缺少 `类型` 字段",
                            "补写 `类型: 协同单/回执单/...`"))
    elif ftype not in DOC_TYPES:
        issues.append(Issue("ERROR", rel, lmap.get("类型", 1), "R3",
                            "非法的文档类型：%r，合法取值 %s" % (ftype, "/".join(DOC_TYPES)),
                            "改为规范取值之一"))

    # R2 必填字段
    base_required = ["类型", "状态", "创建日期", "更新时间"]
    for k in base_required:
        if not data.get(k):
            issues.append(Issue("ERROR", rel, lmap.get(k, 1), "R2",
                                "缺少必填字段 `%s`" % k,
                                "补写 `%s: <按规范取值>`" % k))

    if ftype in VOUCHER_TYPES:
        for k in ["单据编号", "发起人", "接收人"]:
            if not data.get(k):
                issues.append(Issue("ERROR", rel, lmap.get(k, 1), "R2",
                                    "单据缺少必填字段 `%s`" % k,
                                    "补写 `%s`" % k))
        vid = data.get("单据编号", "")
        if vid:
            if not VOUCHER_ID_RE.match(str(vid)):
                issues.append(Issue("ERROR", rel, lmap.get("单据编号", 1), "R4",
                                    "单据编号格式不合法：%r（应为 类型-YYYYMMDD-NNN）" % vid,
                                    "按 `协同单-20261029-001` 格式改写"))
            elif ftype and not str(vid).startswith(ftype):
                issues.append(Issue("ERROR", rel, lmap.get("单据编号", 1), "R3",
                                    "单据编号前缀（%s）与类型（%s）不匹配" % (vid, ftype),
                                    "统一编号前缀与类型"))
            if str(vid).startswith("YYYY") and "模板" not in fname:
                issues.append(Issue("ERROR", rel, lmap.get("单据编号", 1), "R2",
                                    "单据编号仍为占位符",
                                    "填写真实编号"))
    elif ftype:
        if not data.get("文档编号"):
            issues.append(Issue("ERROR", rel, lmap.get("文档编号", 1), "R2",
                                "缺少必填字段 `文档编号`",
                                "补写 `文档编号: <前缀-日期-序号>`"))
        if ftype in ("交付物", "验收结论") and not data.get("负责人"):
            issues.append(Issue("ERROR", rel, lmap.get("负责人", 1), "R2",
                                "缺少必填字段 `负责人`",
                                "补写 `负责人: <成员姓名>`"))
        if ftype in ("动态知识", "静态知识") and not data.get("作者"):
            issues.append(Issue("ERROR", rel, lmap.get("作者", 1), "R2",
                                "缺少必填字段 `作者`",
                                "补写 `作者: <成员姓名>`"))

    # R4 状态合法
    status = data.get("状态")
    if status and status not in STATUS_VALUES:
        issues.append(Issue("ERROR", rel, lmap.get("状态", 1), "R4",
                            "非法的任务状态：%r，合法取值 %s" % (status, "/".join(STATUS_VALUES)),
                            "改为 %s 之一" % "/".join(STATUS_VALUES)))

    # 日期格式
    for k in ("创建日期", "更新时间"):
        v = data.get(k)
        if v and not DATE_RE.match(str(v)) and not str(v).startswith("YYYY"):
            issues.append(Issue("ERROR", rel, lmap.get(k, 1), "R2",
                                "`%s` 格式不合法：%r（应 YYYY-MM-DD）" % (k, v),
                                "改为 2026-10-29 形式"))

    # R5 编号重复
    for k in ("单据编号", "文档编号"):
        v = data.get(k)
        if not v or str(v).startswith("YYYY"):
            continue
        owners = ctx["ids"].get(str(v), [])
        others = [o for o in owners if os.path.abspath(o) != os.path.abspath(path)]
        if others:
            locs = [("交换区" if "/04-交换区/" in o.replace("\\", "/") else
                     "01-输入" if "/01-输入/" in o.replace("\\", "/") else "其他")
                    for o in owners]
            pair_ok = set(locs) <= {"交换区", "01-输入"} and len(owners) == 2
            is_tpl = "模板" in fname
            if not pair_ok and not is_tpl:
                issues.append(Issue("ERROR", rel, lmap.get(k, 1), "R5",
                                    "编号 %s 与其他文件重复（%s）" % (v, ", ".join(
                                        rel_display(o, public_root) for o in owners)),
                                    "保留唯一原件，其余使用副本说明或删除"))

    # R6 人员是否在名册
    roster = ctx["roster"]
    for k in ("发起人", "接收人", "负责人", "作者", "审核人"):
        v = data.get(k)
        if not v or "模板" in fname:
            continue
        v = str(v)
        if v.startswith("<") or v.startswith("{") or v in ("XXX",):
            continue
        if roster and v not in roster:
            issues.append(Issue("ERROR", rel, lmap.get(k, 1), "R6",
                                "`%s` 的值 %r 不在成员名册中（名册：%s）" %
                                (k, v, "/".join(roster)),
                                "更正成员姓名，或先在 `01-成员名册/成员名册-v1.md` 登记"))
    if data.get("发起人") and data.get("接收人") and data["发起人"] == data["接收人"]:
        issues.append(Issue("ERROR", rel, lmap.get("接收人", 1), "R6",
                            "发起人与接收人相同",
                            "接收人应为另一名成员"))

    if ftype:
        check_location(ftype, rel, issues, rel, lmap)
        check_publish(rel, rel, data, lmap, issues, roster, ctx["audit_text"])
        check_links(data, lmap, rel, issues, rel, ctx["files"], ctx["packages"],
                    public_root, rel_root)
        check_ref_singleolders(data, lmap, issues, rel, ctx["ids"], ctx["files"])

    level = "ERROR" if any(i.level == "ERROR" for i in issues) else "OK"
    if issues and level == "OK":
        level = "WARN"
    return issues, level


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    repo_root = None
    if "--repo-root" in argv:
        repo_root = argv[argv.index("--repo-root") + 1]
    targets = []
    if "--stdin" in argv:
        # 从标准输入逐行读取待校验文件路径（供 Hook 安全传递含中文/空格的路径）
        for line in sys.stdin.read().splitlines():
            line = line.strip()
            if line:
                targets.append(line)
    elif "--all" in argv:
        base = repo_root or find_public_root(os.path.abspath(__file__))
        ws = os.path.dirname(base) if base else os.getcwd()
        for root, dirs, fs in os.walk(ws):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".git"]
            for fn in fs:
                if fn.endswith(".md"):
                    targets.append(os.path.join(root, fn))
    else:
        targets = args

    if not targets:
        print("用法：python validate.py [--all | --stdin] [文件.md ...]")
        return 0

    anchor = repo_root or find_public_root(os.path.abspath(targets[0]))
    files, packages, ids, audit_text = scan_index(anchor)
    roster = load_roster(anchor)
    ctx = {"public_root": anchor, "files": files, "packages": packages,
           "ids": ids, "audit_text": audit_text, "roster": roster}

    print("=" * 68)
    print("协作规则校验器 v1 | 公共仓根：%s" % (anchor or "未找到"))
    print("成员名册：%s" % ("/".join(roster) if roster else "<未加载>"))
    print("参与检索文件数：%d，任务包：%d" % (len(files), len(packages)))
    print("=" * 68)

    errors, warns, results = [], [], []
    for t in targets:
        if not os.path.isfile(t):
            results.append((t, "MISSING", []))
            continue
        issues, level = validate_file(t, ctx)
        results.append((rel_display(t, anchor), level, issues))
        for i in issues:
            (errors if i.level == "ERROR" else warns).append(i)

    ok = skipped = 0
    for name, level, issues in results:
        if level == "OK":
            ok += 1
            print("[通过] %s" % name)
        elif level == "SKIP":
            skipped += 1
        elif level == "MISSING":
            print("[跳过] %s（文件不存在）" % name)
        else:
            print("[%s] %s" % ("失败" if level == "ERROR" else "警告", name))
            for i in issues:
                print(i.text())

    print("-" * 68)
    print("通过 %d 个；跳过 %d 个；警告 %d 条；错误 %d 条" %
          (ok, skipped, len(warns), len(errors)))
    if not anchor:
        print("注意：未能定位公共仓根目录，人员名册与引用有效性校验已降级。")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
