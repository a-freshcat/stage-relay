"""Actionable local errors. Never persist exception text or authentication data."""
from urllib.parse import urlsplit, urlunsplit


HELP = {
    "permission_denied": ("本地文件或进程操作被拒绝访问。", "检查下列位置的写入权限和安全软件拦截；项目锁应位于当前用户临时目录。"),
    "file_not_found": ("所需的文件或程序不存在。", "检查下列文件位置；若为 Codex 程序，更新配置中的 codex_command 后重新打开面板。"),
    "endpoint_config": ("模型接口配置或凭据不可用。", "打开“模型与接口”，检查对应角色的接口和认证方式，重新填写或保存密钥。"),
    "endpoint_unavailable": ("无法连接模型接口。", "启动对应的本地网关，核对接口地址和端口；远程接口请检查网络。"),
    "model_catalog_mismatch": ("所选模型不在接口的模型目录中。", "重新读取该 API 的模型列表并选择接口实际返回的模型 ID。"),
    "transport_error": ("模型接口传输失败。", "检查网关运行日志、网络和接口协议；确认接口支持 Responses 请求。"),
    "api_error": ("上游模型接口拒绝了请求。", "查看下列阶段日志和网关日志，检查认证、模型权限、额度及 Responses 兼容性。"),
    "unsupported_parameter": ("上游接口不支持请求中的参数。", "核对网关的 Responses 兼容能力及参数配置。新版 Relay 使用提示词和本地 JSON 校验，不再要求接口支持 text.format: json_schema。"),
    "model_access_denied": ("上游账号无权使用所选模型。", "在对应供应商确认账号是否有该模型权限；选择已有权限的模型，或换用有权限的接口。模型出现在目录中不代表当前账号可以调用。"),
    "network_access_denied": ("上游限制了当前出口 IP 或地区的访问。", "检查该供应商的网络限制和代理出口，或选择可访问的接口；修改 Stage Relay 本地端口不能解除上游限制。"),
    "authentication_error": ("上游接口认证失败或拒绝访问。", "检查对应接口的 API Key、登录状态和访问权限。"),
    "upstream_rate_limit": ("上游请求受到限流。", "检查供应商额度和并发限制，稍后再试；不会自动切换到其他模型。"),
    "process_or_api_error": ("模型执行进程或接口返回了错误。", "展开该角色的公开错误和进程诊断，并核对网关日志；检查登录、模型能力和接口兼容性。"),
    "cli_initialization_error": ("Codex 本地执行环境初始化失败，尚未开始模型推理。", "请从正常 Windows 用户环境启动面板；从宿主受限进程启动时可能无法访问 Codex 的本地状态或通信设施。保留模型进程的沙箱与审批设置，查看 stderr.log 中的初始化错误。"),
    "protocol_error": ("配置文件或模型返回的数据不符合接力格式。", "查看阶段输入、response.json 和 validation-error.json；模型输出由本地校验，接口无需支持 json_schema。格式修复使用现有 max_fix_rounds 配置且不会重新执行文件修改。"),
    "format_repair_modified_project": ("修复 JSON 报告时项目发生了变化。", "检查项目差异及阶段日志；格式修复应只读，不应重复执行任务。"),
    "timeout": ("本阶段等待超时。", "检查网关响应和进程日志；可在配置中调整该角色的 timeout_seconds。"),
    "request_limit": ("达到配置的请求次数上限。", "检查累计请求记录及对应角色的 max_requests；恢复任务不会清空累计次数。"),
    "input_limit": ("请求大小超过配置限制。", "检查请求记录和 max_request_bytes 配置。"),
    "config_error": ("本地接力配置无效。", "检查配置文件，或在“模型与接口”中重新保存角色配置。"),
    "catalog_error": ("接口模型目录检查失败。", "核对接口地址、凭据和 GET /models 支持情况；此检查未发起生成请求。"),
    "check_modified_project": ("兼容测试的临时样例被改动，检查结果不可信。", "重新运行兼容测试；若反复出现，查看 Codex 是否在样例中写入了文件。"),
    "empty_model_answer": ("模型进程结束但没有返回内容。", "检查接口协议、模型是否静默或被网关拦截；可先运行最小生成测试。"),
    "local_error": ("本地操作失败，旧记录没有保存具体异常。", "此旧记录无法还原全部细节。新版会在新的失败记录中保存阶段、位置、异常类型和处理建议。"),
}


def describe_failure(exc, context=None, logs=None):
    context = context or {}
    code = getattr(exc, "code", None)
    if not code:
        code = ("permission_denied" if isinstance(exc, PermissionError) else
                "file_not_found" if isinstance(exc, FileNotFoundError) else
                "timeout" if isinstance(exc, TimeoutError) else
                "local_io_error" if isinstance(exc, OSError) else "internal_error")
    message, action = HELP.get(code, ("任务在下列阶段失败（" + code + "）。", "查看下列阶段、位置及本地日志，按错误代码检查配置或运行环境。"))
    row = {"code": code, "message": message, "action": action,
           "exception": type(exc).__name__, "stage": context.get("stage", "本地任务初始化")}
    for key in ("role", "model", "path", "executable", "phase"):
        if context.get(key):
            row[key] = str(context[key])
    if type(context.get("upstream_status")) is int:
        row["upstream_status"] = context["upstream_status"]
    endpoint = context.get("endpoint")
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            row["endpoint"] = urlunsplit((parsed.scheme, (parsed.hostname or "") + (":" + str(parsed.port) if parsed.port else ""), parsed.path, "", ""))
        except ValueError:
            pass
    # Use only an OS filename, never str(exc), which may include a request/key.
    if isinstance(exc, OSError):
        for key in ("errno", "winerror"):
            if getattr(exc, key, None) is not None:
                row[key] = getattr(exc, key)
        if exc.filename and "path" not in row:
            row["path"] = str(exc.filename)
    if logs:
        row["logs"] = [str(path) for path in logs]
    return row
