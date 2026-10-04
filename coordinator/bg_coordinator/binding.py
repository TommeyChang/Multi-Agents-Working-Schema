"""工程绑定——**体系管机制，工程管落点**。

## 为什么要这一层

体系（`agents/`／`rules/`）只该写**换一个工程还成立**的东西：角色形与属、权限、状态机、
不变量、交接面。而"哪几条线、工作面是哪些路径、门禁跑什么、受保护资产叫什么"——
这些**换个工程就不成立**，写进体系里就是**看起来像规则、实际是某一工程的巧合**
（错得不像错的，是这里最贵的失败形态）。

所以把它们收进一份**绑定**：一个工程一份，字段固定、可校验、缺项要报。

## 放哪

| 位置 | 什么时候 |
|---|---|
| `<主干>/.maws/project.md` | **优先**——随工程版本走，可评审、可 diff、可回滚（设计上的正位） |
| `<体系根>/bindings/<工程目录名>.md` | 体系侧登记（**当前工程用这条**：改动面限定在体系内，不动生产仓） |

解析顺序固定：主干侧优先，体系侧兜底。将来把绑定搬进主干，**机制一行不用改**。

## 格式

人读部分随便写；**机读部分是一个 ```json 块**（只认第一个）。
必需字段由 `schema.BINDING_FIELDS` 声明——**那是唯一权威**，本模块只按它校验，不自己另立一份。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: 主干侧的相对位置（设计正位）
TRUNK_REL = Path(".maws") / "project.md"

#: 体系侧的登记目录
BINDINGS_DIRNAME = "bindings"

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)

#: 来源标记
SRC_TRUNK = "主干侧"
SRC_MAWS = "体系侧"
SRC_NONE = "未登记"

#: `migrations` 里**闸真正读**的那两个子字段 ——（子字段, 用途）。
#:
#: 为什么单独管：人读部分写了"基线 `origin/main`"、机读块里却漏了，是**最阴的缺口形态**——
#: 看起来声明了，闸却取不到，于是"含迁移件的提交"全被拒（或 [6] 静默不生效）。
#: 判据只钉**读它的代码用到的那两个**，不钉 `tool`／`naming`／`gotchas`（那是给人看的）。
MIGRATION_SUBFIELDS: tuple[tuple[str, str], ...] = (
    ("dir", "迁移目录：闸据此找件"),
    ("base", "基线：新增件接在谁后面／哪些件算已落库"),
)


def maws_root() -> Path:
    """体系根（`bg_coordinator/binding.py` 往上三层）。"""
    return Path(__file__).resolve().parents[2]


def bindings_dir() -> Path:
    return maws_root() / BINDINGS_DIRNAME


def candidates(repo: Path) -> list[tuple[str, Path]]:
    """候选绑定路径，**按优先级**排：主干侧 → 体系侧。"""
    return [
        (SRC_TRUNK, repo / TRUNK_REL),
        (SRC_MAWS, bindings_dir() / f"{repo.name}.md"),
    ]


def locate(repo: Path | None) -> tuple[str, Path | None]:
    """找绑定：返回 `(来源, 路径)`；都没有则 `(未登记, None)`。"""
    if repo is None:
        return SRC_NONE, None
    for source, path in candidates(repo):
        if path.is_file():
            return source, path
    return SRC_NONE, None


def extract_json(text: str) -> tuple[dict | None, str]:
    """取**第一个** ```json 块。取不到／不是对象 ⇒ 明确报错，不静默当空。"""
    m = _JSON_BLOCK.search(text)
    if not m:
        return None, "文件里没有 ```json 机读块"
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        return None, f"机读块不是合法 JSON：{exc}"
    if not isinstance(data, dict):
        return None, "机读块的根必须是对象"
    return data, ""


def load(path: Path) -> tuple[dict | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"读不到：{exc}"
    return extract_json(text)


def validate(data: dict, fields: tuple[tuple[str, str, str], ...]) -> list[str]:
    """按 `schema.BINDING_FIELDS` 校验；返回**缺口清单**（空 = 完整）。

    三种缺口都报：缺字段、类型不符、**空值**（空 dict／空 list／空串）。
    「有字段但空着」是最危险的形态——它看起来像已经声明了。
    `migrations` 再往下一层钉两个**闸真正读**的子字段（见 `MIGRATION_SUBFIELDS`）。
    """
    gaps: list[str] = []
    for name, kind, desc in fields:
        if name not in data:
            gaps.append(f"缺字段 `{name}`（{desc}）")
            continue
        value = data[name]
        if kind == "str" and not (isinstance(value, str) and value.strip()):
            gaps.append(f"`{name}` 须为非空字符串（{desc}）")
        elif kind == "int" and not isinstance(value, int):
            gaps.append(f"`{name}` 须为整数（{desc}）")
        elif kind == "dict" and not (isinstance(value, dict) and value):
            gaps.append(f"`{name}` 须为非空对象（{desc}）")
        elif kind == "list" and not (isinstance(value, list) and value):
            gaps.append(f"`{name}` 须为非空数组（{desc}）")
    mig = data.get("migrations")
    if isinstance(mig, dict) and mig:
        for sub, desc in MIGRATION_SUBFIELDS:
            if not (isinstance(mig.get(sub), str) and mig[sub].strip()):
                gaps.append(f"`migrations.{sub}` 须为非空字符串（{desc}）")
    return gaps


def line_of(data: dict | None, code: str) -> dict | None:
    """某条线在绑定里的声明（缺绑定或缺该线 ⇒ None）。"""
    if not data:
        return None
    lines = data.get("lines")
    if not isinstance(lines, dict):
        return None
    entry = lines.get(code)
    return entry if isinstance(entry, dict) else None


def shadows(repo: Path | None, taken: Path | None, adopted: dict | None) -> list[dict]:
    """找出**未被采用**的候选副本——两处并存是「第二真相源」的入口。

    返回每一项带 `diverged`：
    - 与已采用的那份**机读块不一致** ⇒ `diverged=True`（**静默漂移**，必须报缺口）；
    - 一致 ⇒ 只是冗余副本（报出来，但不阻断）。

    为什么不能只是"按优先级取一份就完事"：被忽略的那份**不会停止变化**。
    它今天一致，下个月就不一致，而没有任何机制会提醒任何人。
    """
    out: list[dict] = []
    if repo is None:
        return out
    for source, path in candidates(repo):
        if path == taken or not path.is_file():
            continue
        other, err = load(path)
        out.append(
            {
                "source": source,
                "path": str(path),
                "error": err,
                "diverged": bool(err) or (other != adopted),
            }
        )
    return out


def describe(repo: Path | None, fields: tuple[tuple[str, str, str], ...]) -> dict:
    """给 `coord bind` 用的完整状态：来源／路径／缺口／各线工作面／**并存副本**。"""
    source, path = locate(repo)
    out: dict = {
        "project": repo.name if repo else "",
        "source": source,
        "path": str(path) if path else "",
        "gaps": [],
        "status": "未登记",
        "lines": {},
        "shadowed": [],
    }
    if path is None:
        out["gaps"] = [f"未登记绑定：主干侧 {TRUNK_REL} 不存在，体系侧 {BINDINGS_DIRNAME}/ 也没有"]
        return out
    data, err = load(path)
    if data is None:
        out["gaps"] = [err]
        return out
    out["gaps"] = validate(data, fields)
    out["shadowed"] = shadows(repo, path, data)
    for shadow in out["shadowed"]:
        if shadow["diverged"]:
            out["gaps"].append(
                f"另有{shadow['source']}副本且与已采用的不一致（{shadow['path']}）——"
                "两处并存必然静默漂移：**只留一处**，别让被忽略的那份继续变"
            )
        else:
            out["gaps"].append(
                f"另有{shadow['source']}冗余副本（{shadow['path']}）——内容暂时一致，"
                "但它会漂移：搬完就删旧，别留副本"
            )
    out["status"] = "完整" if not out["gaps"] else "有缺口"
    lines = data.get("lines")
    if isinstance(lines, dict):
        out["lines"] = {
            str(k): (v.get("workface") if isinstance(v, dict) else None)
            for k, v in lines.items()
        }
    return out


def scan_registered(fields: tuple[tuple[str, str, str], ...]) -> list[dict]:
    """扫描体系侧登记目录——**体系自己的不变量**：登记了就必须自洽。

    用于 `reconcile()`：绑定文件写坏了（漏机读块、缺字段、空值）要在对账时报出来，
    而不是等到某个工程派单时才发现。
    """
    out: list[dict] = []
    d = bindings_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        data, err = load(path)
        record = {"file": path.name, "gaps": [err] if data is None else validate(data, fields)}
        if data is not None:
            record["project"] = str(data.get("project", ""))
            record["lines"] = sorted((data.get("lines") or {}).keys())
        out.append(record)
    return out
