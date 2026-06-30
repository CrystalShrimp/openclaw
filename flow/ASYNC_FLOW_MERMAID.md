# OpenClaw 异步运行流程图 (Mermaid)

## 图例

### 箭头类型

| 箭头 | 含义 |
|------|------|
| `──▶` 实线 | 同步/直接调用 |
| `══▶` 粗线 | await（阻塞等待，调用者挂起直到完成） |
| `- -▶` 虚线 | create_task（异步委托，调用者立即继续） |

### 颜色编码

| 颜色 | 代码 | 含义 | 出现位置 |
|------|------|------|----------|
| 🔵 浅蓝 | `#cce5ff` | **飞书侧** — 飞书服务器、用户交互、消息发送 | 流程二、四、六、七、全链路 |
| 🟢 浅绿 | `#d4edda` | **常驻后台 Task** — WS 接收循环、心跳、卡片回调、审批 resolve | 流程一、二、三、七 |
| 🟡 浅黄 | `#fff3cd` | **异步委托 / create_task 起点** — 调度方、按需 Task | 流程一、二、三、四、五、七 |
| 🔴 浅红 | `#f8d7da` | **Hook 审批流程** — 跨进程审批、HTTP 回调、风险工具 | 流程二、四、六、七 |
| ⚪ 浅灰 | `#f8f9fa` | **Claude CLI 子进程** — 独立进程、不受 asyncio 管理 | 流程五、六、七 |
| 🟣 浅紫 | `#e8d5e7` | **子进程管理层** — cli_loop、per-user Lock | 流程五、全链路 |
| 🔴 亮红 | `#ff6b6b` | **Future 阻塞点 [F]** — await 挂起，等待外部 resolve | 流程四、六、全链路 |
| 🔵 极浅蓝 | `#e8f4fd` | **JSONL 事件解析区** — 读取主循环内部的事件分发 | 流程五 |

---

## 一、服务启动

> 🟡 黄 = create_task 起点 → 🟢 绿 = 常驻后台 Task → `await _select()` 保活重连容器

```mermaid
flowchart TD
    uvicorn["uvicorn.run('app.main:app')"]
    lifespan["lifespan() — FastAPI 生命周期"]
    create_ws["create_ws_client()"]
    ws_init["FeishuWsClient.__init__()"]
    start_async["ws_client.start_async()"]
    run_forever["_run_forever()"]
    connect["_connect()"]
    recv_loop["_receive_message_loop() 🟢常驻Task"]
    ping_loop["_ping_loop() 🟢常驻Task, 10s心跳"]
    select["_select() 🟢永久阻塞<br/>保活 _run_forever 以支持重连"]
    fastapi["yield → FastAPI 启动 HTTP 服务"]

    uvicorn --> lifespan
    lifespan --> create_ws
    create_ws --> ws_init
    lifespan --> start_async
    start_async -.->|"🟡create_task"| run_forever
    lifespan -->|"start_async 立即返回"| fastapi
    run_forever ==>|"await"| connect
    connect -.->|"🟡create_task"| recv_loop
    run_forever -.->|"🟡create_task"| ping_loop
    run_forever ==>|"await"| select

    style start_async fill:#fff3cd
    style run_forever fill:#fff3cd
    style recv_loop fill:#d4edda
    style ping_loop fill:#d4edda
    style select fill:#d4edda
    style fastapi fill:#cce5ff
```

**`_select()` 保活解释：** `_receive_message_loop` 和 `_ping_loop` 已经是独立 Task，不需要 `_select()` 来维持。但 `_run_forever` 是重连的兜底容器（`try/except → _reconnect`），如果它 return 了，重连机制就失效。所以 `_select()` 把 `_run_forever` 钉在 `try` 块里。

---

## 二、消息接收流

> 🔵 蓝 = 飞书侧 → 🟢 绿 = 常驻 Task → 🟡 黄 = create_task 起点 → 🔴 红 = 转入卡片流程

```mermaid
flowchart TD
    feishu["🔵 飞书服务器<br/>WS 推送二进制帧"]

    subgraph loop["🟢 常驻后台 Task"]
        recv["_receive_message_loop() 🟢"]
        handle["- -▶ _handle_message()"]
        dataframe["_handle_data_frame()"]
        parse["解析 protobuf frame"]
        check_type{"message_type?"}

        on_msg["on_message_receive(event)"]
        parse_msg["解析 open_id, text"]
        dispatch["- -▶ _dispatch() 🟡"]
        write["_write_message()<br/>发送 ACK (< 3s)"]

        event_handler["event_handler<br/>.do_without_validation(pl)"]
    end

    feishu --> recv
    recv -.->|"🟡create_task"| handle
    handle ==>|"await"| dataframe
    dataframe --> parse
    parse --> check_type
    check_type -->|"EVENT"| event_handler
    check_type -->|"CARD"| card_flow["🔴 → 流程三: 卡片回调"]

    event_handler -->|"同步调用"| on_msg
    on_msg --> parse_msg
    parse_msg -.->|"🟡create_task"| dispatch
    on_msg -->|"立即返回"| write

    style feishu fill:#cce5ff
    style dispatch fill:#fff3cd
    style card_flow fill:#f8d7da
    style recv fill:#d4edda
```

---

## 三、卡片回调流

> 🟢 绿 = WS 快速返回 → 🟡 黄 = create_task 后台处理 → 🔵 蓝 = 飞书回调

```mermaid
flowchart TD
    subgraph ws_handler["WS 处理 (必须在 3s 内返回)"]
        card_action["on_card_action(event)"]
        parse_card["解析 approval_id, act, card_type"]
        check_type{"card_type?"}
        model_sel["model_selection:<br/>session.last_model = next"]
        profile_switch["profile_switch:<br/>switch_profile() + cancel_by_user()"]
        tool_approve["工具审批"]
        create_task_hd["- -▶ approval_manager<br/>.handle_decision()"]
        build_resp["构造 Response<br/>P2CardActionTriggerResponse()<br/>空构造 → 属性赋值"]
        return_resp["return response (< 3s)"]
    end

    subgraph background["后台 Task (不阻塞 WS 返回)"]
        handle_dec["approval_manager<br/>.handle_decision()"]
        update_status["更新 request.status"]
        audit["audit_logger.log()"]
        resolve["future.set_result(approved)<br/>解除 [F] 阻塞点"]
    end

    card_action --> parse_card
    parse_card --> check_type
    check_type -->|"model_selection"| model_sel
    check_type -->|"profile_switch"| profile_switch
    check_type -->|"工具审批"| tool_approve
    model_sel -.->|"create_task"| create_task_hd
    profile_switch -.->|"cancel_by_user"| kill_proc["kill CLI 进程"]
    tool_approve -.->|"create_task"| create_task_hd
    card_action --> build_resp
    build_resp --> return_resp

    create_task_hd --> handle_dec
    handle_dec --> update_status
    update_status --> audit
    audit --> resolve

    style return_resp fill:#d4edda
    style resolve fill:#fff3cd
    style card_action fill:#cce5ff
```

---

## 四、命令分发流

> 🟡 黄 = create_task 起点 → 🔴 红 = 模型选择卡片 → 🟢 绿 = 最终执行

```mermaid
flowchart TD
    dispatch["_dispatch(open_id, chat_id, text)<br/>由 create_task 调度"]

    cmd_start["/start / /new → reset_user_session()"]
    cmd_stop["/stop → cancel_and_wait()"]
    cmd_status["/status → send_text()"]
    cmd_switch["/switch → profile 选择卡片"]
    cmd_model["/model → session.last_model"]
    cmd_mode["/mode → session.approval_mode"]
    cmd_pwd["/pwd → session.workspace"]
    cmd_resume["/resume → session.claude_session_id"]
    cmd_continue["/continue → _run_claude(skip_classify)"]
    cmd_compact["/compact → _run_claude('/compact')"]
    cmd_mem["/mem → 写入 AGENT.md"]
    cmd_sh["/sh → 子进程执行命令"]
    cmd_clean["/clean → 清理旧 session 文件"]
    cmd_default["普通文本 → _run_claude()"]

    run_claude["_run_claude(prompt, open_id, session, skip_classify)"]

    path_a{"路径选择"}
    path_a_desc["A: 有 last_model<br/>(手动 /model 或上次选择)"]
    path_b["B: 首次消息, 无历史"]
    path_c["C: skip_classify=true<br/>(/continue, /compact 等)"]

    model_card["弹出模型选择卡片<br/>build_model_selection_card()"]
    future_block["await Future [F]<br/>等待用户点击模型选择卡片"]
    exec_cli["→ 执行 CLI"]

    dispatch --> cmd_start
    dispatch --> cmd_stop
    dispatch --> cmd_status
    dispatch --> cmd_switch
    dispatch --> cmd_model
    dispatch --> cmd_mode
    dispatch --> cmd_pwd
    dispatch --> cmd_resume
    dispatch --> cmd_continue
    dispatch --> cmd_compact
    dispatch --> cmd_mem
    dispatch --> cmd_sh
    dispatch --> cmd_clean
    dispatch --> cmd_default

    cmd_continue --> run_claude
    cmd_compact --> run_claude
    cmd_default --> run_claude

    run_claude --> path_a
    path_a -->|"A"| path_a_desc
    path_a -->|"B"| path_b
    path_a -->|"C"| path_c

    path_a_desc --> exec_cli
    path_b --> model_card
    model_card ==>|"await"| future_block
    future_block --> exec_cli
    path_c --> exec_cli

    style dispatch fill:#cce5ff
    style run_claude fill:#fff3cd
    style future_block fill:#f8d7da
    style exec_cli fill:#d4edda
```

---

## 五、CLI 子进程执行流

> 🟣 紫 = 子进程管理 → ⚪ 灰 = Claude CLI 子进程 → 🔵极浅蓝 = JSONL 事件解析

```mermaid
flowchart TD
    subgraph uvicorn_process["uvicorn 进程 (asyncio Event Loop)"]
        cli_loop["claude_cli_loop.run()"]
        lock_check{"per-user Lock<br/>是否锁定?"}
        lock_error["return error<br/>'上一个任务未完成'"]
        run_inner["_run_inner()"]

        build_args["构建 CLI args"]
        send_init_card["send_card()<br/>build_streaming_card()"]
        env_fix["env.pop('CLAUDECODE')<br/>检测 git-bash"]
        hook_config["_ensure_hook_config()"]

        send_pid["send_text('PID: xxx')"]
        drain_stderr["- -▶ _drain_stderr()"]

        subgraph jsonl_loop["JSONL 读取主循环"]
            readline["await proc.stdout.readline()"]
            parse_line["json.loads(line)"]

            sys_init["system/init:<br/>记录 session_id<br/>注册 session_registry"]
            stream["stream_event (text_delta):<br/>累积文本, 500ms 节流更新卡片"]
            assistant_tool["assistant (tool_use):<br/>记录 ToolCallRecord"]
            assistant_text["assistant (text):<br/>accumulated_text +="]
            user_ignore["user: (忽略)"]
            result["result:<br/>记录 cost/duration<br/>update_card()"]
            api_retry["system/api_retry:<br/>send_text('重试中')"]
        end

        proc_wait["await proc.wait()"]
        error_card["无输出? → build_error_card"]
        cleanup["cleanup session_registry"]
        return_result["return AgentResult"]
    end

    cli_loop --> lock_check
    lock_check -->|"已锁定"| lock_error
    lock_check -->|"未锁定"| run_inner
    run_inner ==>|"async with lock"| build_args
    build_args --> send_init_card
    send_init_card --> env_fix
    env_fix --> hook_config
    hook_config ==>|"create_subprocess_exec"| subprocess

    subgraph subprocess["claude CLI 子进程"]
        cli["claude -p ... --output-format stream-json"]
        stdout["stdout: JSONL 流"]
        stderr["stderr: 错误输出"]
    end

    subprocess --> send_pid
    send_pid -.->|"create_task"| drain_stderr
    send_pid --> readline

    readline ==>|"await"| parse_line
    parse_line --> sys_init
    parse_line --> stream
    parse_line --> assistant_tool
    parse_line --> assistant_text
    parse_line --> user_ignore
    parse_line --> result
    parse_line --> api_retry
    result --> proc_wait
    readline -->|"EOF"| proc_wait
    proc_wait --> error_card
    error_card --> cleanup
    cleanup --> return_result

    style lock_check fill:#fff3cd
    style subprocess fill:#f8f9fa
    style jsonl_loop fill:#e8f4fd
    style drain_stderr fill:#fff3cd
```
**事件流：
---

## 六、工具审批流（跨三进程）

> 🟡 黄 = Claude CLI 子进程 → 🔴 红 = Hook 审批 → 🟢 绿 = FastAPI → 🔵 蓝 = 飞书用户

```mermaid
flowchart TD
    subgraph claude_cli["① Claude CLI 子进程"]
        tool_prep["准备执行工具<br/>(如 Bash: rm -rf /tmp/test)"]
        read_settings["读取 .claude/settings.json<br/>发现 PreToolUse hook"]
        spawn_hook["启动 hook 子进程<br/>stdin: tool_name + tool_input"]
        read_stdout["读取 hook stdout<br/>→ allow: 执行工具<br/>→ deny: 跳过工具"]
        continue_output["继续输出 JSONL<br/>→ 回到流程五读取循环"]
    end

    subgraph hook_script["② pre_tool_use.py (hook 子进程)"]
        read_stdin["json.load(sys.stdin)"]
        http_post["urllib.request.urlopen()<br/>POST /hooks/pre_tool_use<br/>同步阻塞, 最长 1800s"]
        print_result["print(json.dumps(result))<br/>→ stdout 输出决策"]
    end

    subgraph fastapi["③ FastAPI (uvicorn 进程)"]
        hook_endpoint["pre_tool_use(request)"]
        check_mode{"审批模式?"}
        allow_all["return allow<br/>(mode=h 或 低风险工具)"]
        request_approval["request_tool_approval()"]
        create_future["创建 Future [F]"]
        send_approval_card["await send_card()<br/>发送审批卡片到飞书"]
        await_future["await Future<br/>阻塞等待用户操作"]
        return_decision["return allow/deny"]

        expire["- -▶ _expire_after()<br/>超时自动拒绝"]
    end

    subgraph feishu_user["④ 飞书用户"]
        card["看到审批卡片"]
        click_btn["点击 允许/拒绝"]
    end

    tool_prep --> read_settings
    read_settings --> spawn_hook
    spawn_hook --> read_stdin
    read_stdin --> http_post

    http_post --> hook_endpoint
    hook_endpoint --> check_mode
    check_mode -->|"h 或 低风险"| allow_all
    check_mode -->|"l 或 高风险"| request_approval
    request_approval --> create_future
    create_future -.->|"create_task"| expire
    create_future ==>|"await"| send_approval_card
    send_approval_card --> card
    card --> click_btn
    click_btn -->|"流程三<br/>handle_decision"| resolve_future["future.set_result()"]
    resolve_future --> await_future
    await_future --> return_decision

    return_decision --> print_result
    print_result --> read_stdout
    read_stdout --> continue_output

    style claude_cli fill:#fff3cd
    style hook_script fill:#f8d7da
    style fastapi fill:#d4edda
    style feishu_user fill:#cce5ff
    style await_future fill:#f8d7da
    style resolve_future fill:#fff3cd
```

---

## 七、并发模型总览

> 🟢 绿 = 常驻 → 🔵 蓝 = 按需 → 🟡 黄 = 后台 → 🔴 红 = 外部进程

```mermaid
flowchart TD
    subgraph event_loop["单线程 asyncio Event Loop (uvicorn)"]
        subgraph resident["常驻 Task"]
            r1["_receive_msg_loop<br/>(WS 接收)"]
            r2["_ping_loop<br/>(WS 心跳)"]
            r3["_select()<br/>(阻塞保活)"]
        end

        subgraph on_demand["按需 Task (每条消息一个)"]
            d1["_dispatch #1<br/>(用户A)<br/>per-user Lock"]
            d2["_dispatch #2<br/>(用户B)<br/>per-user Lock"]
            d3["handle_decision<br/>(审批回调)<br/>future.resolve"]
        end

        subgraph background["后台 Task"]
            b1["_drain_stderr<br/>(读CLI错误输出)"]
            b2["_expire_after ×N<br/>(审批超时倒计时)"]
        end
    end

    subgraph external["外部进程"]
        e1["claude.exe<br/>(CLI 子进程)"]
        e2["python<br/>(hook 脚本子进程)"]
    end

    d1 -.->|"create_subprocess"| e1
    e1 -.->|"启动 hook"| e2

    style event_loop fill:#f8f9fa
    style resident fill:#d4edda
    style on_demand fill:#cce5ff
    style background fill:#fff3cd
    style external fill:#f8d7da
```

---

## 全链路总览 (一图流)

> 🔵蓝=飞书 → 🟢绿=WS → 🟡黄=分发 → 🟣紫=子进程管理 → ⚪灰=Claude CLI → 🔴红=Hook → 亮红=Future

```mermaid
flowchart LR
    subgraph feishu["飞书"]
        user["用户消息"]
        card_btn["卡片按钮"]
    end

    subgraph ws["WebSocket"]
        recv["on_message_receive"]
        card_action["on_card_action"]
    end

    subgraph dispatch["分发"]
        cmd_router["_dispatch"]
        run_claude["_run_claude"]
        model_card["模型选择卡片"]
    end

    subgraph cli_loop["子进程管理"]
        lock["per-user Lock"]
        spawn["create_subprocess_exec"]
        jsonl_read["readline JSONL"]
    end

    subgraph claude["Claude CLI"]
        execute["执行任务"]
        hook_trigger["触发 PreToolUse hook"]
    end

    subgraph hook["Hook 审批"]
        hook_script["pre_tool_use.py"]
        http_post["POST /hooks/pre_tool_use"]
        approval["approval_manager"]
        future["Future [F]"]
    end

    subgraph profiles["Profile 切换"]
        switch["/switch"]
        profile_card["profile 选择卡片"]
        copy_settings["copy settings_*.json"]
    end

    user -->|"WS 推送"| recv
    recv -.->|"create_task"| cmd_router
    cmd_router --> run_claude
    run_claude -->|"首次消息"| model_card
    model_card -->|"用户选择"| run_claude
    run_claude --> lock
    lock --> spawn
    spawn --> jsonl_read
    jsonl_read --> execute
    execute --> hook_trigger
    hook_trigger --> hook_script
    hook_script -->|"HTTP"| http_post
    http_post --> approval
    approval --> future
    future -->|"send_card"| card_btn
    card_btn -->|"WS 推送"| card_action
    card_action -.->|"create_task"| resolve["future.resolve"]
    resolve --> future
    future --> hook_script
    hook_script --> execute
    execute --> jsonl_read

    switch --> profile_card
    profile_card --> copy_settings
    copy_settings -->|"cancel_by_user"| lock

    style feishu fill:#cce5ff
    style ws fill:#d4edda
    style dispatch fill:#fff3cd
    style cli_loop fill:#e8d5f5
    style claude fill:#f8f9fa
    style hook fill:#f8d7da
    style future fill:#ff6b6b,color:#fff
    style profiles fill:#e8d5e7
```
