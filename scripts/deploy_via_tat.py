#!/usr/bin/env python3
"""通过腾讯云自动化助手（TAT）在 Lighthouse 实例上执行命令，替代 SSH 直连。

为什么用它
----------
GitHub Actions 的托管 runner 出口 IP 在美国，用 SSH 登录生产机时腾讯云主机安全会
持续产生「异地登录 root」高危告警 —— 部署越频繁告警越密集。把 runner 的 IP 段加进
白名单也不可行：GitHub 只发布一个会变动的 CIDR 池（api.github.com/meta 的 actions
字段），无法固定。

TAT 的方式是**下发**而不是登录：命令进入云端队列，实例上的 agent 主动拉取并执行，
全程没有任何入站连接。因此既不产生登录审计记录，也可以把 22 端口对公网关闭。

接口说明
--------
用 `tat` 产品接口（版本 2020-10-28），Content 为命令文本的 base64，上限 64 KB。
下发返回执行活动 ID（inv-xxx），再用 DescribeInvocationTasks 按 invocation-id 轮询
任务状态，SUCCESS 才认为部署成功。

用法
----
    TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=... \\
        python scripts/deploy_via_tat.py --region ap-shanghai \\
            --instance-id lhins-8k8ahx8m --command-file /tmp/cmd.sh --timeout 3600

退出码：0 = 命令在实例上执行成功；1 = 失败 / 超时 / 状态异常。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HOST = "tat.tencentcloudapi.com"
SERVICE = "tat"
API_VERSION = "2020-10-28"

# TAT 的硬限制：Content（base64 后的命令）不超过 64 KB
MAX_CONTENT_BYTES = 64 * 1024

# 这些状态表示任务尚未结束，需要继续轮询
RUNNING_STATES = frozenset({"PENDING", "DELIVERING", "RUNNING"})


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def call_api(
    action: str,
    payload: dict[str, Any],
    *,
    secret_id: str,
    secret_key: str,
    region: str,
    token: str | None = None,
) -> dict[str, Any]:
    """调用一次腾讯云 API（TC3-HMAC-SHA256 签名，仅依赖标准库）。"""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    now = int(time.time())
    date = dt.datetime.fromtimestamp(now, tz=dt.UTC).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"

    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            f"content-type:{content_type}",
            f"host:{HOST}",
            "",
            "content-type;host",
            _sha256_hex(body),
        ]
    )
    scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join(["TC3-HMAC-SHA256", str(now), scope, _sha256_hex(canonical_request.encode("utf-8"))])

    k_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    k_service = _hmac_sha256(k_date, SERVICE)
    k_signing = _hmac_sha256(k_service, "tc3_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers = {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders=content-type;host, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(now),
        "X-TC-Version": API_VERSION,
    }
    if region:
        headers["X-TC-Region"] = region
    if token:
        headers["X-TC-Token"] = token

    request = urllib.request.Request(f"https://{HOST}/", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"[tat] {action} 调用失败（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"[tat] 无法连接 {HOST}：{exc.reason}") from exc

    data = json.loads(raw)
    result = data.get("Response", {})
    if "Error" in result:
        err = result["Error"]
        raise SystemExit(f"[tat] {action} 返回错误：{err.get('Code')} - {err.get('Message')}")
    return result


def run_command(
    command: str,
    *,
    instance_id: str,
    region: str,
    secret_id: str,
    secret_key: str,
    token: str | None,
    timeout: int,
    working_directory: str,
) -> str:
    """下发命令，返回执行活动 ID（inv-xxx）。"""
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_CONTENT_BYTES:
        raise SystemExit(
            f"[tat] 命令内容 {len(encoded)} 字节，超过 TAT 的 64 KiB 上限。"
            "请拆分载荷（例如先下发配置文件，再下发执行命令）。"
        )

    response = call_api(
        "RunCommand",
        {
            "Content": encoded,
            "InstanceIds": [instance_id],
            "CommandType": "SHELL",
            "WorkingDirectory": working_directory,
            "Timeout": timeout,
            # 不保存命令历史：避免部署脚本内容长期留在控制台
            "SaveCommand": False,
        },
        secret_id=secret_id,
        secret_key=secret_key,
        region=region,
        token=token,
    )
    invocation_id = response.get("InvocationId", "")
    if not invocation_id:
        raise SystemExit(f"[tat] 下发未返回 InvocationId：{response}")
    print(f"[tat] 已下发 → 实例 {instance_id}，执行活动 {invocation_id}（超时 {timeout}s）")
    return invocation_id


def wait_for_task(
    invocation_id: str,
    *,
    region: str,
    secret_id: str,
    secret_key: str,
    token: str | None,
    poll_interval: int,
    max_wait: int,
) -> dict[str, Any]:
    """轮询执行任务，直到离开运行中状态或到达本地等待上限。"""
    started = time.monotonic()
    last_status = ""
    while True:
        response = call_api(
            "DescribeInvocationTasks",
            {
                "Filters": [{"Name": "invocation-id", "Values": [invocation_id]}],
                # 必须显式关闭隐藏，否则拿不到命令输出，排障时无从下手
                "HideOutput": False,
                "Limit": 10,
            },
            secret_id=secret_id,
            secret_key=secret_key,
            region=region,
            token=token,
        )
        tasks = response.get("InvocationTaskSet") or []
        if not tasks:
            raise SystemExit(f"[tat] 查不到执行活动 {invocation_id} 的任务记录")

        task = tasks[0]
        status = str(task.get("TaskStatus", ""))
        elapsed = int(time.monotonic() - started)
        if status != last_status:
            print(f"[tat] 状态 {status}（已等待 {elapsed}s）")
            last_status = status

        if status not in RUNNING_STATES:
            return task

        if elapsed > max_wait:
            print(f"[tat] 本地等待已达上限 {max_wait}s，任务仍在 {status}；不再轮询。")
            return task

        time.sleep(poll_interval)


def report(task: dict[str, Any]) -> int:
    """打印任务结果，返回进程退出码。"""
    status = str(task.get("TaskStatus", ""))
    result = task.get("TaskResult") or {}
    exit_code = result.get("ExitCode")
    dropped = result.get("Dropped") or 0

    output = ""
    raw_output = result.get("Output")
    if raw_output:
        try:
            output = base64.b64decode(raw_output).decode("utf-8", "replace")
        except (ValueError, TypeError):
            output = "<输出无法解码>"

    if output:
        print("\n----- 实例输出开始 -----")
        print(output.rstrip())
        print("----- 实例输出结束 -----")
        if dropped:
            print(f"[tat] 注意：输出有 {dropped} 字节被丢弃（TAT 回传上限），完整日志请上机查看")

    error_info = task.get("ErrorInfo")
    if error_info:
        print(f"[tat] 错误信息：{error_info}")

    print(f"\n[tat] 最终状态 {status}，退出码 {exit_code}")
    return 0 if status == "SUCCESS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过腾讯云 TAT 在 Lighthouse 实例上执行命令")
    parser.add_argument("--region", required=True, help="地域，如 ap-shanghai")
    parser.add_argument("--instance-id", required=True, help="Lighthouse 实例 ID，如 lhins-8k8ahx8m")
    parser.add_argument("--command-file", required=True, help="包含待执行 Shell 命令的文件路径")
    parser.add_argument("--timeout", type=int, default=3600, help="云端执行超时（秒），默认 3600")
    parser.add_argument("--working-directory", default="/opt/stockmaster", help="工作目录，默认 /opt/stockmaster")
    parser.add_argument("--poll-interval", type=int, default=15, help="轮询间隔（秒），默认 15")
    parser.add_argument("--max-wait", type=int, default=3600, help="本地最长等待（秒），默认 3600")
    args = parser.parse_args(argv)

    secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
    secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    token = os.environ.get("TENCENTCLOUD_TOKEN") or None
    if not secret_id or not secret_key:
        raise SystemExit("[tat] 缺少 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY 环境变量")

    command_path = Path(args.command_file)
    if not command_path.is_file():
        raise SystemExit(f"[tat] 命令文件不存在：{command_path}")
    command = command_path.read_text(encoding="utf-8")
    if not command.strip():
        raise SystemExit(f"[tat] 命令文件为空：{command_path}")

    print(f"[tat] 命令 {len(command)} 字节，来自 {command_path}")
    invocation_id = run_command(
        command,
        instance_id=args.instance_id,
        region=args.region,
        secret_id=secret_id,
        secret_key=secret_key,
        token=token,
        timeout=args.timeout,
        working_directory=args.working_directory,
    )
    task = wait_for_task(
        invocation_id,
        region=args.region,
        secret_id=secret_id,
        secret_key=secret_key,
        token=token,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
    )
    return report(task)


if __name__ == "__main__":
    sys.exit(main())
