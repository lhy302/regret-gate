"""RiskRouter：参数级工具调用风险分级（构建规范 §4）。

只按工具名分类 = 错误（§15.2）。本模块结合工具名 + 调用参数 + 命令内容 + 路径合法性：
- 命令拼接（`&&` `;` `|`）必须拆分，分别判定，**取最严**；
- 路径遍历（`..`、越出允许根目录、命中禁止根目录）必须拦截；
- 未匹配任何规则 → `default_level`（默认 `intercept`，保守兜底）；
- 非字符串参数不参与 `arg_pattern` 匹配，按 `default_level` 处理；
- 每条决策都打 `matched_rule` 标签，供误拦截率统计（§4.5）。
"""

from __future__ import annotations

import os
import re
from typing import Optional

from core.types import (
    RISK_ALLOW,
    RISK_BUFFER,
    RISK_INTERCEPT,
    RiskDecision,
    ToolCall,
    risk_severity,
)

SHELL_SPLIT_RE = re.compile(r"&&|\|\||;|\|")

RULE_ID_PREFIX = "rule:"

DEFAULT_ALLOWED_ROOTS = ["."]
DEFAULT_FORBIDDEN_ROOTS = ["C:\\Windows", "C:\\Program Files", "/etc", "/usr", "/bin", "/sys"]

# 提取 Windows 绝对路径 / UNC 路径 / 类 Unix 绝对路径 / 含 .. 的相对路径
WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'|;&)]*")
UNC_PATH_RE = re.compile(r"\\\\[^\s\"'|;&)]+")
UNIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s\"'|;&)]+)")
# 例：../../etc/passwd、..\..\Windows\System32 —— 必须能提取出来才能做射程判定
RELATIVE_TRAVERSAL_RE = re.compile(r"(?:\.\.[\\/])+[^\s\"'|;&)]*")


def _looks_like_path(command: str) -> bool:
    lowered = command.lower()
    return ".." in command or "~" in command or lowered.strip().startswith(("cd ", "pushd", "set-location"))


class RiskPolicy:
    """可组合的规则列表 + 兜底级别（§4.2）。"""

    def __init__(
        self,
        rules: Optional[list] = None,
        default_level: str = RISK_INTERCEPT,
        case_sensitive: bool = False,
        allowed_roots: Optional[list] = None,
        forbidden_roots: Optional[list] = None,
        split_compound_commands: bool = True,
    ):
        self.rules = list(rules or [])
        self.default_level = default_level
        self.case_sensitive = case_sensitive
        self.allowed_roots = list(allowed_roots or DEFAULT_ALLOWED_ROOTS)
        self.forbidden_roots = list(forbidden_roots or DEFAULT_FORBIDDEN_ROOTS)
        self.split_compound_commands = split_compound_commands

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskPolicy":
        cfg = cfg or {}
        return cls(
            rules=cfg.get("rules", []),
            default_level=cfg.get("default_level", RISK_INTERCEPT),
            case_sensitive=bool(cfg.get("case_sensitive", False)),
            allowed_roots=cfg.get("allowed_roots"),
            forbidden_roots=cfg.get("forbidden_roots"),
            split_compound_commands=bool(cfg.get("split_compound_commands", True)),
        )

    # -- 规则匹配 ---------------------------------------------------------

    def _match(self, rule: dict, tool_name: str, args: dict) -> bool:
        cond = rule.get("condition", {})
        want_tool = cond.get("tool_name")
        if want_tool is not None and want_tool != tool_name:
            return False
        pattern = cond.get("arg_pattern")
        if pattern is None:
            return True
        field = cond.get("arg_field", "command")
        value = args.get(field)
        if not isinstance(value, str):
            return False
        flags = 0 if self.case_sensitive else re.IGNORECASE
        return re.search(pattern, value.strip(), flags) is not None

    def classify_segment(self, tool_name: str, args: dict, segment: Optional[str] = None) -> RiskDecision:
        """对单个命令片段（或整体参数）判定。"""
        probe_args = dict(args)
        if segment is not None:
            field = "command"
            for rule in self.rules:
                if rule.get("condition", {}).get("tool_name") == tool_name:
                    field = rule.get("condition", {}).get("arg_field", "command")
                    break
            probe_args[field] = segment

        for idx, rule in enumerate(self.rules):
            if self._match(rule, tool_name, probe_args):
                return RiskDecision(
                    level=rule.get("level", self.default_level),
                    reason=rule.get("reason", "matched rule"),
                    matched_rule=self._rule_label(rule, idx) if segment is None else f"{self._rule_label(rule, idx)}[{segment}]",
                    rule_id=self._rule_id(rule, idx),
                )
        return RiskDecision(
            level=self.default_level,
            reason=(
                f"no rule matched for tool={tool_name!r}"
                + (f" segment={segment!r}" if segment is not None else "")
                + " -> default level"
            ),
            matched_rule=None,
            rule_id=None,
        )

    @staticmethod
    def _rule_id(rule: dict, idx: int) -> str:
        return rule.get("id") or f"{RULE_ID_PREFIX}{idx}:{rule.get('condition', {}).get('tool_name', '*')}"

    @staticmethod
    def _rule_label(rule: dict, idx: int) -> str:
        cond = rule.get("condition", {})
        label = rule.get("id")
        if not label:
            label = f"{RULE_ID_PREFIX}{idx}:{cond.get('tool_name', '*')}"
            if cond.get("arg_pattern"):
                label += f":{cond['arg_pattern']}"
        return label

    # -- 路径检查 ---------------------------------------------------------

    def extract_paths(self, command: str) -> list:
        paths = WIN_PATH_RE.findall(command)
        paths += UNC_PATH_RE.findall(command)
        paths += [p for p in UNIX_PATH_RE.findall(command) if not p.startswith("//")]
        paths += RELATIVE_TRAVERSAL_RE.findall(command)
        return paths

    def check_paths(self, command: str, cwd: str = ".") -> Optional[str]:
        """返回违规原因；合法时返回 None（§4.4 路径遍历 / 越权路径）。"""
        base = os.path.abspath(cwd)
        allowed = [os.path.abspath(os.path.join(base, r)) for r in self.allowed_roots]
        forbidden = [os.path.abspath(os.path.join(base, r)) if not os.path.isabs(r) else os.path.normpath(r)
                     for r in self.forbidden_roots]

        def contained(path: str, roots: list) -> bool:
            p = os.path.normcase(os.path.normpath(path))
            for root in roots:
                r = os.path.normcase(os.path.normpath(root))
                if p == r or p.startswith(r + os.sep):
                    return True
            return False

        for raw in self.extract_paths(command):
            if ".." in raw or raw.startswith("~"):
                # 遍历类路径永远不在允许范围内（§4.4）
                return f"path traversal detected: {raw!r}"
            candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
            try:
                normalized = os.path.normpath(candidate)
            except (ValueError, OSError):
                return f"unresolvable path: {raw!r}"
            if contained(normalized, forbidden) and not contained(normalized, allowed):
                return f"path outside allowed roots: {normalized!r}"
        return None


class RiskRouter:
    """（§4.1）`classify(tool_call) -> RiskDecision`。"""

    def __init__(self, policy: Optional[RiskPolicy] = None, registry: Optional[dict] = None):
        if policy is None:
            from core.tool_call_parser import default_registry

            registry = registry if registry is not None else default_registry()
            policy = RiskPolicy(
                rules=[
                    {"condition": {"tool_name": "read_file"}, "level": RISK_ALLOW, "reason": "read-only file access"},
                    {"condition": {"tool_name": "write_file"}, "level": RISK_BUFFER, "reason": "bufferable write"},
                    {"condition": {"tool_name": "draft.commit_revision"}, "level": RISK_ALLOW, "reason": "in-memory revision registration"},
                    {"condition": {"tool_name": "http_request", "arg_field": "method", "arg_pattern": "^(GET|HEAD)$"}, "level": RISK_ALLOW, "reason": "idempotent http read"},
                    {"condition": {"tool_name": "http_request"}, "level": RISK_BUFFER, "reason": "http request with side effects"},
                    {"condition": {"tool_name": "execute_shell", "arg_pattern": "^(ls|cat|Get-Content|git status|git diff)"}, "level": RISK_ALLOW, "reason": "read-only shell command"},
                    {"condition": {"tool_name": "execute_shell", "arg_pattern": "^(rm|Remove-Item|del|git push --force|git reset --hard)"}, "level": RISK_INTERCEPT, "reason": "destructive shell command"},
                    {"condition": {"tool_name": "execute_shell"}, "level": RISK_BUFFER, "reason": "shell command with unknown effect"},
                ],
                default_level=RISK_INTERCEPT,
            )
        self.policy = policy
        self._default_registry = registry

    @classmethod
    def from_config(cls, cfg: dict, registry: Optional[dict] = None) -> "RiskRouter":
        cfg = cfg or {}
        policy_cfg = cfg.get("risk_policy", cfg)
        return cls(policy=RiskPolicy.from_config(policy_cfg), registry=registry)

    def classify(self, tool_call: ToolCall, explain: bool = False):
        """参数级风险分级。

        `explain=True` 时返回 `(strictest_decision, 分段决策列表, 越权路径标记)`，
        供误拦截判定复用**同一套判定逻辑**，避免两处实现漂移。
        """
        name = tool_call.name
        args = tool_call.args or {}

        if tool_call.malformed:
            decision = RiskDecision(
                level=RISK_INTERCEPT,
                reason=f"malformed tool arguments: {tool_call.malformed_reason}",
                matched_rule="malformed_arguments",
                rule_id="malformed_arguments",
            )
            return (decision, [decision], False) if explain else decision

        decisions: list = []
        path_flags: list = []

        # 1) 整体判定（含非字符串参数走 default_level 的情形）
        overall = self.policy.classify_segment(name, args)
        decisions.append(overall)
        path_flags.append(False)

        # 2) 命令拼接：拆分后逐段判定，取最严（只对 shell 命令生效）
        command = args.get("command")
        if name == "execute_shell" and isinstance(command, str) and self.policy.split_compound_commands:
            segments = [s.strip() for s in SHELL_SPLIT_RE.split(command) if s.strip()]
            if segments:
                for seg in segments:
                    decisions.append(self.policy.classify_segment(name, args, segment=seg))
                    path_flags.append(False)
            # 3) 路径合法性
            violation = self.policy.check_paths(command, cwd=args.get("cwd") or ".")
            if violation:
                decisions.append(
                    RiskDecision(
                        level=RISK_INTERCEPT,
                        reason=violation,
                        matched_rule="path_scope",
                        rule_id="path_scope",
                    )
                )
                path_flags.append(True)

        # 非字符串命令参数（例如 command 传了数组/对象）→ 保守走 default_level
        if name == "execute_shell" and "command" in args and not isinstance(command, str):
            decisions.append(
                RiskDecision(
                    level=self.policy.default_level,
                    reason=f"non-string command argument ({type(command).__name__}) -> default level",
                    matched_rule="non_string_arg",
                    rule_id="non_string_arg",
                )
            )
            path_flags.append(False)

        strictest = decisions[0]
        for decision in decisions[1:]:
            if risk_severity(decision.level) > risk_severity(strictest.level):
                strictest = decision
        return (strictest, decisions, any(path_flags)) if explain else strictest

    @staticmethod
    def _mentions_dangerous_operation(segment: str, declared: list) -> bool:
        """该命令片段是否与任务标注的高危操作对齐。

        - 单操作名（如 `Remove-Item`）：按可执行名前缀对齐；
        - 多词操作（如 `git reset --hard`）：**必须整条短语对齐** —— 危险面是整条指令，
          若只比对 `git` 会把 `git stash list` 也当成高危，误拦截率会被算低。
        """
        operation = " ".join(segment.lower().split())
        if not operation:
            return False
        for entry in declared:
            normalized = " ".join(entry.split())
            if " " in normalized:
                if operation.startswith(normalized):
                    return True
            elif operation.startswith(normalized.split()[0]):
                return True
        return False

    def is_false_intercept(self, tool_call: ToolCall, dangerous_operations: list) -> bool:
        """误拦截判定（§13.1 / §4.5）：被判 intercept，但任务标注里并没有对应的高危操作。

        判定复用 `classify(explain=True)`，口径只有一条、可解释、不依赖对分段索引的反推：

        - 命令被拆成若干段后，**只要有任意一段**与任务标注的 `dangerous_operations`
          对齐（长前缀优先，因此 `git reset --hard` 不会被简化成 `git`）→ 真拦截；
        - 否则 → 误拦截。

        按此口径，“只读命令 + 路径越权”被判 intercept 会**明确计为误拦截**：
        真值基准是任务自己声明的高危操作（“该操作会不会造成不可逆损害”），
        而路径策略过严属于 harness 侧需要修的规则问题，正是误拦截率要暴露的东西。
        """
        strictest, _decisions, _path_violation = self.classify(tool_call, explain=True)
        if strictest.level != RISK_INTERCEPT:
            return False

        declared = [str(op).strip().lower() for op in (dangerous_operations or []) if str(op).strip()]
        if not declared:
            return True
        command = (tool_call.args or {}).get("command")
        if not isinstance(command, str) or not command.strip():
            # 非 shell 操作（或参数非法）被判 intercept：无对应标注即视为误拦截
            return True
        segments = [s for s in (part.strip() for part in SHELL_SPLIT_RE.split(command)) if s]
        return not any(self._mentions_dangerous_operation(s, declared) for s in segments)
