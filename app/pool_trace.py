"""池化检测阳性溯源（临时预览，不改写审查记录）。

从历史审查记录中**仅选择一条时效正常**的记录，读取其**当前配方**（两版
记录取标签核对对象，即新版配方；单版取现版）配料树中的**叶子原料**
（没有子配料的节点；同名原料出现在多处时按归一化名去重，展示名取首次
出现者），作为池化检测结果中允许引用的原料全集。

用户逐行录入检测池，每行格式为::

    池名：原料列表：阳性/阴性

- 冒号中英文均可，必须恰好给出三段；原料之间用逗号 / 顿号 / 分号 /
  空白分隔（与字典批量导入口径一致）；
- 状态只接受精确的「阳性」或「阴性」（大小写不敏感仅针对英文，中文须
  逐字匹配，「+」「阳性 」之类写法一律视为非法状态）；
- 池名不能为空、同一批内池名（去空白后）不得重复；
- 原料列表中引用的每种原料都必须是该记录当前配方的叶子原料，引用非
  配方原料时整批拒绝并定位到行；同一池内重复引用的原料自动去重。
- ``#`` 开头的行与空行忽略。

推理规则：

- **阴性池**排除其全部原料：出现在任一阴性池中的原料不可能是污染源，
  从嫌疑候选中剔除；
- **阳性池**须至少包含一个嫌疑原料：其全部原料都被阴性证据排除（含
  阳性池本身原料列表为空）时，该阳性结果无法解释，返回无解并列出
  **冲突池**及其被哪些阴性池排除；
- 后端用**子集动态规划**（状态数 = 2^|阳性池|）求能解释全部阳性池
  结果的**最少嫌疑原料集合**（命中集 / set cover：每个阳性池至少含
  一个所选原料）；被阴性池排除或不出现在任何阳性池中的原料不参与求解；
- 最少嫌疑原料数并列时，统计**全部并列解**的数量，并按所选原料名
  升序序列的**字典序**展示唯一最小解；
- 同时列出每个嫌疑原料**解释的阳性池**（包含该原料的阳性池，按录入
  顺序）与每个阳性池的命中嫌疑原料。

没有录入任何阳性池时，空集合即为唯一最少解（阴性池仍做行校验与原样
回显）。记录缺失（不存在 / 已删除）或处于**需复核**状态（旧记录无
指纹、相关原料映射已变更）时拒绝溯源并说明原因。整个模块只读：不
写入任何检测池，也不改写任何审查记录。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import db
from .analysis import normalize_name
from .changeover import blocked_message

_SPLIT_INGREDIENTS_RE = re.compile(r"[,，、;；\s]+")
_SPLIT_COLON_RE = re.compile(r"[:：]")

# 状态词 → 标准状态；只接受精确写法（去首尾空白后）
_STATUS_WORDS = {"阳性": "positive", "阴性": "negative"}
_STATUS_LABEL = {"positive": "阳性", "negative": "阴性"}


class PoolTraceError(ValueError):
    """池化溯源请求无法完成（映射为 HTTP 400）。

    problems 逐条列出原因：记录资格问题（缺失 / 需复核 / ID 无效）或
    检测池行解析问题（行号、类型、原文），供页面定位。
    """

    def __init__(self, message: str, problems: Optional[List[dict]] = None):
        super().__init__(message)
        self.problems = problems or []


# ---------- 记录资格与叶子原料 ----------

def list_trace_records() -> List[dict]:
    """候选记录清单：每条历史记录的当前配方叶子原料与选择资格。"""
    records = []
    for summary in db.list_reviews():
        review = db.get_review(summary["id"])
        needs = bool(review.get("needs_recheck"))
        reason = review.get("recheck_reason")
        leaves = leaf_ingredients_of(review)
        records.append({
            "id": review["id"],
            "name": review["name"],
            "created_at": review["created_at"],
            "has_two_versions": bool(review.get("text_b")),
            "source_version": _source_version(review),
            "leaf_ingredients": leaves,
            "leaf_count": len(leaves),
            "eligible": not needs,
            "blocked_reason": blocked_message(reason) if needs else None,
        })
    records.sort(key=lambda r: r["id"])
    return records


def _source_version(review: dict) -> str:
    result = review.get("result") or {}
    return result.get("label_check_version") or review.get("label_a") or "现版配方"


def _current_tree(review: dict) -> List[dict]:
    """记录当前配方（标签核对对象：有新版取新版，否则取现版）的配料树。"""
    result = review.get("result") or {}
    version = result.get("version_b") or result.get("version_a") or {}
    return version.get("tree", [])


def leaf_ingredients_of(review: dict) -> List[dict]:
    """当前配方树的全部叶子原料（无子配料节点），按归一化名去重。

    同名原料出现在不同分支 / 层级时视为同一种原料：展示名取先序遍历
    中的首次出现者，并记录出现次数与全部叶子路径。结果按展示名排序，
    供候选记录列表与求解统一口径。
    """
    merged: Dict[str, dict] = {}
    order: List[str] = []

    def walk(nodes: List[dict], prefix: tuple) -> None:
        for node in nodes:
            name = node.get("name", "")
            path = prefix + (name,)
            children = node.get("children", [])
            if not children:
                key = normalize_name(name)
                hit = merged.get(key)
                if hit is None:
                    merged[key] = {"name": name, "occurrences": 1,
                                  "paths": [list(path)]}
                    order.append(key)
                else:
                    hit["occurrences"] += 1
                    hit["paths"].append(list(path))
            else:
                walk(children, path)

    walk(_current_tree(review), ())
    leaves = [merged[key] for key in order]
    leaves.sort(key=lambda item: item["name"])
    return leaves


def _record_problem(review_id, reason: str, message: str,
                    name: Optional[str] = None) -> dict:
    return {"id": review_id, "name": name, "reason": reason, "message": message}


def _load_eligible_review(review_id) -> dict:
    """取出记录并校验选择资格，返回记录本身（叶子原料由调用方提取）。"""
    try:
        rid = int(review_id)
    except (TypeError, ValueError):
        raise PoolTraceError(
            "记录 ID 无效，禁止溯源",
            problems=[_record_problem(review_id, "invalid",
                                      "记录 ID 无效，禁止选择")])
    review = db.get_review(rid)
    if review is None:
        raise PoolTraceError(
            "审查记录不存在或已删除，禁止溯源",
            problems=[_record_problem(rid, "missing",
                                      "审查记录不存在或已删除，禁止选择")])
    if review.get("needs_recheck"):
        reason = review.get("recheck_reason")
        raise PoolTraceError(
            "记录处于需复核状态，禁止溯源",
            problems=[_record_problem(
                review["id"], reason or "unknown",
                f"记录{blocked_message(reason)}，禁止选择",
                name=review["name"])])
    return review


# ---------- 检测池行解析 ----------

def parse_pools(text: str, leaves: List[dict]) -> List[dict]:
    """解析「池名：原料列表：阳性/阴性」逐行文本。

    leaves 为该记录当前配方的叶子原料（:func:`leaf_ingredients_of`
    的返回值）。任一行不合法即抛出 :class:`PoolTraceError`，problems
    给出**全部**问题行（与别名批量导入、套餐规划的整批拒绝口径一致：
    本次预览不产生任何结论）。返回保持录入顺序的检测池列表::

        [{line, name, ingredients(展示名去重排序), keys(set),
          status("positive"/"negative")}]
    """
    leaf_names = {normalize_name(item["name"]): item["name"] for item in leaves}
    pools: List[dict] = []
    problems: List[dict] = []
    seen: Dict[str, int] = {}
    for idx, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _SPLIT_COLON_RE.split(line)
        if len(parts) != 3:
            problems.append({
                "line": idx, "type": "format",
                "message": "格式应为「池名：原料列表：阳性/阴性」（需恰好 2 个冒号分隔 3 段）",
            })
            continue
        name = parts[0].strip()
        if not name:
            problems.append({"line": idx, "type": "empty_name",
                             "message": "池名不能为空"})
            continue
        status_word = parts[2].strip()
        status = _STATUS_WORDS.get(status_word)
        if status is None:
            problems.append({
                "line": idx, "type": "bad_status",
                "message": f"检测状态必须是「阳性」或「阴性」（收到 {status_word!r}）",
            })
            continue
        if name in seen:
            problems.append({
                "line": idx, "type": "duplicate_name",
                "message": f"池名「{name}」与第 {seen[name]} 行重复，池名必须唯一",
            })
            continue
        seen[name] = idx
        # 原料按归一化名去重；同池内重复引用折叠为一个，展示名取首次写法
        keys: List[str] = []
        displays: Dict[str, str] = {}
        unknown: List[str] = []
        for token in _SPLIT_INGREDIENTS_RE.split(parts[1]):
            raw = token.strip()
            if not raw:
                continue
            key = normalize_name(raw)
            display = leaf_names.get(key)
            if display is None:
                unknown.append(raw)
            elif key not in displays:
                displays[key] = display
                keys.append(key)
        if unknown:
            problems.append({
                "line": idx, "type": "unknown_ingredient",
                "message": "池「{}」引用了非当前配方叶子原料：{}".format(
                    name, "、".join(sorted(set(unknown)))),
            })
            continue
        ingredients = [displays[key] for key in keys]
        ingredients.sort()
        pools.append({"line": idx, "name": name, "ingredients": ingredients,
                      "keys": set(keys), "status": status})
    if problems:
        raise PoolTraceError(
            f"检测池录入有 {len(problems)} 行未通过校验，请修正后重试",
            problems=problems)
    return pools


# ---------- 阴性排除 / 阳性冲突 ----------

def _negative_exclusions(pools: List[dict]):
    """汇总阴性证据：被排除原料 → 排除它的阴性池（按录入顺序）。"""
    excluded: Dict[str, List[str]] = {}
    for pool in pools:
        if pool["status"] != "negative":
            continue
        for key in pool["keys"]:
            excluded.setdefault(key, []).append(pool["name"])
    return excluded


def _conflict_positive_pools(pools: List[dict],
                             excluded: Dict[str, List[str]],
                             leaf_display: Dict[str, str]) -> List[dict]:
    """全部原料都被阴性证据排除（或列表为空）的阳性池，即无法解释者。"""
    conflicts = []
    for pool in pools:
        if pool["status"] != "positive":
            continue
        survivor_keys = [k for k in pool["keys"] if k not in excluded]
        if survivor_keys:
            continue
        if not pool["keys"]:
            detail = {"reason": "empty", "excluded_ingredients": [],
                      "excluded_by": {}}
        else:
            names = sorted(leaf_display[k] for k in pool["keys"])
            detail = {
                "reason": "excluded",
                "excluded_ingredients": names,
                "excluded_by": {
                    leaf_display[k]: list(excluded[k])
                    for k in sorted(pool["keys"], key=leaf_display.get)},
            }
        conflicts.append({"line": pool["line"], "name": pool["name"], **detail})
    return conflicts


# ---------- 最少嫌疑原料集合（命中集子集 DP） ----------

def solve_trace(pools: List[dict], leaves: List[dict]) -> dict:
    """求解释全部阳性池的最少嫌疑原料集合。

    阴性池先排除其全部原料；某阳性池的全部原料都被阴性证据排除（或其
    列表为空）时返回 feasible=False 与冲突池。否则把阳性池按录入顺序
    编号为位掩码目标位，候选原料为「出现在至少一个阳性池中且未被阴性
    池排除」的叶子原料，按原料名排序。0/1 背包式子集 DP 求最少命中
    原料数与并列解数，再以**后缀 DP + 按原料名升序贪心**重建字典序
    最小的唯一原料名序列。
    """
    positives = [p for p in pools if p["status"] == "positive"]
    negatives = [p for p in pools if p["status"] == "negative"]
    leaf_display = {normalize_name(item["name"]): item["name"]
                    for item in leaves}
    excluded = _negative_exclusions(pools)

    m = len(positives)
    conflicts = _conflict_positive_pools(pools, excluded, leaf_display)
    pos_key_sets = [set(p["keys"]) - set(excluded) for p in positives]
    # 候选原料：出现于阳性池且未被阴性排除；按展示名升序固定处理顺序
    candidate_keys = sorted(
        {key for keys in pos_key_sets for key in keys},
        key=lambda k: leaf_display[k])

    if conflicts:
        return _build_result(
            feasible=False, pools=pools, positives=positives,
            negatives=negatives, leaf_display=leaf_display,
            excluded=excluded, candidate_keys=candidate_keys,
            chosen=None, optimal_count=0, m=m, conflicts=conflicts)

    if m == 0:
        # 没有任何阳性池：空集合即唯一最少解
        return _build_result(
            feasible=True, pools=pools, positives=positives,
            negatives=negatives, leaf_display=leaf_display,
            excluded=excluded, candidate_keys=candidate_keys,
            chosen=[], optimal_count=1, m=m)

    full = (1 << m) - 1
    masks: List[int] = []
    for key in candidate_keys:
        mask = 0
        for i, keys in enumerate(pos_key_sets):
            if key in keys:
                mask |= 1 << i
        masks.append(mask)
    n = len(candidate_keys)

    # 0/1 背包：dp[mask] = 恰好命中 mask 个阳性池的 (最少原料数, 并列解数)，
    # 不可达为 None。候选原料均命中至少一个未被排除的阳性池（mask != 0），
    # 但加入后可能不带来新的命中：所选原料数只增不减，冗余选择不可能更优，
    # 永不进入最少解（正成本 0/1 背包的标准剪枝）。
    dp: List[Optional[Tuple[int, int]]] = [None] * (1 << m)
    dp[0] = (0, 1)
    for pmask in masks:
        snapshot = list(dp)  # 每种原料至多选一次
        for mask, state in enumerate(snapshot):
            if state is None:
                continue
            nmask = mask | pmask
            cand_size = state[0] + 1
            cur = dp[nmask]
            if cur is None or cand_size < cur[0]:
                dp[nmask] = (cand_size, state[1])
            elif cand_size == cur[0]:
                dp[nmask] = (cur[0], cur[1] + state[1])

    best = dp[full]
    # 无冲突时每个阳性池都至少有一个未被排除的候选原料，其并集必覆盖
    # 全部目标位，full 必然可达；不可达属于数据不一致，防御性报错。
    if best is None:  # pragma: no cover
        raise PoolTraceError("阳性池结果无法由候选原料解释")
    min_size, ways = best

    # 后缀 DP：suf[k][mask] = 仅用候选原料 [k:] 把已命中 mask 补到 full
    # 所需的最少追加原料数（不可达为 None）。随后按原料名升序贪心重建
    # 字典序最小序列：已能以预算补全即停止（空续接是前缀，字典序最小）；
    # 否则取「加入后仍能以最少原料数补全」的最小名原料。
    suf: List[List[Optional[int]]] = [
        [None] * (1 << m) for _ in range(n + 1)]
    suf[n][full] = 0
    for k in range(n - 1, -1, -1):
        pmask = masks[k]
        for mask in range(1 << m):
            skip = suf[k + 1][mask]
            tail = suf[k + 1][mask | pmask]
            take = tail + 1 if tail is not None else None
            if skip is None:
                chosen = take
            elif take is None:
                chosen = skip
            else:
                chosen = skip if skip < take else take
            suf[k][mask] = chosen

    chosen_idx: List[int] = []
    covered = 0
    used = 0
    k = 0
    while not (covered == full and used == min_size):
        for j in range(k, n):
            tail = suf[j + 1][covered | masks[j]]
            if tail is not None and used + 1 + tail == min_size:
                chosen_idx.append(j)
                covered |= masks[j]
                used += 1
                k = j + 1
                break
        else:  # pragma: no cover - 可行性已由正向 DP 保证
            raise RuntimeError("最少嫌疑集合重建失败")

    return _build_result(
        feasible=True, pools=pools, positives=positives,
        negatives=negatives, leaf_display=leaf_display,
        excluded=excluded, candidate_keys=candidate_keys,
        chosen=[candidate_keys[i] for i in chosen_idx],
        optimal_count=ways, m=m)


def _pool_overview(pool: dict, excluded: Dict[str, List[str]],
                   leaf_display: Dict[str, str], chosen_set: set,
                   m: int, pos_index: Dict[str, int]) -> dict:
    """录入检测池的原样回显：阴性池给出排除原料，阳性池给出命中嫌疑。"""
    item = {
        "line": pool["line"],
        "name": pool["name"],
        "status": pool["status"],
        "status_label": _STATUS_LABEL[pool["status"]],
        "ingredients": list(pool["ingredients"]),
    }
    if pool["status"] == "negative":
        item["excludes"] = list(pool["ingredients"])
        item["exclude_count"] = len(pool["ingredients"])
    else:
        hit_keys = sorted(
            (k for k in pool["keys"] if k in chosen_set),
            key=lambda k: leaf_display[k])
        item["suspects"] = [leaf_display[k] for k in hit_keys]
        item["suspect_count"] = len(hit_keys)
        survivor_keys = [k for k in pool["keys"] if k not in excluded]
        item["survivors"] = sorted(leaf_display[k] for k in survivor_keys)
    return item


def _build_result(*, feasible: bool, pools: List[dict], positives: List[dict],
                  negatives: List[dict], leaf_display: Dict[str, str],
                  excluded: Dict[str, List[str]], candidate_keys: List[str],
                  chosen: Optional[List[str]], optimal_count: int, m: int,
                  conflicts: Optional[List[dict]] = None) -> dict:
    """组装溯源结果（检测池原样回显 + 嫌疑结论 / 冲突）。"""
    chosen_set = set(chosen or [])
    pos_index = {p["name"]: i for i, p in enumerate(positives)}
    overviews = [
        _pool_overview(p, excluded, leaf_display, chosen_set, m, pos_index)
        for p in pools]

    excluded_overview = [
        {"ingredient": leaf_display[key],
         "excluded_by": list(pool_names)}
        for key, pool_names in sorted(excluded.items(),
                                     key=lambda kv: leaf_display[kv[0]])]
    result = {
        "feasible": feasible,
        "pools": overviews,
        "positive_pools": [p["name"] for p in positives],
        "negative_pools": [p["name"] for p in negatives],
        "excluded_ingredients": excluded_overview,
        "candidate_ingredients": sorted(leaf_display[k]
                                        for k in candidate_keys),
        "positive_count": m,
        "negative_count": len(negatives),
    }
    if not feasible:
        result.update({
            "min_suspect_count": None,
            "optimal_count": 0,
            "suspects": [],
            "conflicts": conflicts or [],
        })
        return result

    suspects = []
    for key in chosen or []:
        explained = [p["name"] for p in positives if key in p["keys"]]
        suspects.append({
            "ingredient": leaf_display[key],
            "explains": explained,
            "explain_count": len(explained),
        })
    result.update({
        "min_suspect_count": len(chosen or []),
        "optimal_count": optimal_count,
        "suspects": suspects,
        "conflicts": [],
    })
    return result


# ---------- 编排 ----------

def build_pool_trace_preview(review_id, text: str) -> dict:
    """池化溯源预览的完整编排：校验记录资格后解析检测池并推理（只读）。

    记录缺失或处于需复核状态时抛出 :class:`PoolTraceError`，problems
    给出禁止选择的原因；检测池行解析问题同样整批拒绝并逐行指出；阳性
    池被阴性证据完全排除时返回 feasible=False 与冲突池（非异常，属
    正常的无解结论）。
    """
    review = _load_eligible_review(review_id)
    leaves = leaf_ingredients_of(review)
    pools = parse_pools(text, leaves)
    if not pools:
        raise PoolTraceError(
            "至少录入一个检测池（每行「池名：原料列表：阳性/阴性」）",
            problems=[{"line": None, "type": "empty",
                       "message": "检测池内容为空（# 注释 / 空行不计入），请至少录入一行"}])

    result = solve_trace(pools, leaves)
    result["review"] = {
        "id": review["id"],
        "name": review["name"],
        "source_version": _source_version(review),
        "leaf_ingredients": leaves,
    }
    return result
