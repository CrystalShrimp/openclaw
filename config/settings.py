from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # Claude Code CLI
    claude_cli_path: str = "claude"
    claude_default_model: str = "sonnet"

    # Workspace
    default_workspace: str = "D:\\projects"

    # Approval
    approval_timeout: int = 600           # Model selection card timeout (10 min)
    tool_approval_timeout: int = 1800     # Tool execution card timeout (30 min)
    tool_approval_warn_seconds: int = 300 # Warn 5 min before tool approval expires
    approval_mode: str = "m"  # h=高容忍 m=中(高风险审批) l=低(全审批)

    # Access control
    allowed_users: str = ""

    # Audit
    audit_log_path: str = "./audit.log"

    # Context monitoring
    context_warn_percent: int = 80       # warn when context usage exceeds this %
    context_critical_percent: int = 95   # critical threshold, suggest /new

    # Compact feature (summarize conversation and continue in new session)
    compact_enabled: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    def get_allowed_users(self) -> list[str]:
        return [u.strip() for u in self.allowed_users.split(",") if u.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
