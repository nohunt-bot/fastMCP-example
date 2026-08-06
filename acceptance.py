#!/usr/bin/env python3
"""驗收測試執行器 —— 專為「便宜/弱模型」設計的機械化流程。

設計原則（為什麼不是直接叫人跑 pytest）：

1. **不需要判斷力。** 每一項只有 通過 / 失敗 兩種結果，失敗時直接印出
   「原因 + 下一步該做什麼」，不需要看懂 Python traceback。
2. **不需要記憶。** 每組測試前面印出「這組在驗什麼」，執行者不必先讀懂架構。
3. **可分段執行。** `--group 安全` 只跑一組，弱模型一次只處理一件事，
   不會因為輸出太長而失焦。
4. **結束一定有明確結論。** 最後一行永遠是「全部通過」或
   「N 項失敗，先修第一項」，不留下模稜兩可的狀態。

用法：

    uv run python acceptance.py              # 全部
    uv run python acceptance.py --group 安全  # 只跑一組
    uv run python acceptance.py --list       # 列出所有組別
    uv run python acceptance.py --smoke      # 只做啟動冒煙測試（最快）

離開碼：0 = 全部通過，1 = 有失敗，2 = 環境有問題（還沒開始測）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass
class Item:
    """一個驗收項目。"""

    name: str          # 中文名稱
    selector: str      # pytest -k 用的表達式
    why: str           # 這項在保護什麼
    on_fail: str       # 失敗時該怎麼辦


@dataclass
class Group:
    title: str
    purpose: str
    items: list[Item] = field(default_factory=list)


GROUPS: list[Group] = [
    Group(
        title="索引",
        purpose="確認 skill 能被找到、卡片夠小、快取會在檔案變動時失效。",
        items=[
            Item("能掃到 skills/ 底下的 skill", "test_index_finds_the_example_skills",
                 "找不到 skill 等於整台服務沒用。",
                 "檢查 --skills 指到的目錄，以及每個 skill 資料夾裡有沒有 SKILL.md。"),
            Item("卡片不含 skill 內文", "test_cards_carry_no_body",
                 "卡片一旦夾帶內文，list_skills 會炸掉 context。",
                 "檢查 index.py 的 card()，只能有 name/description/tags/scripts。"),
            Item("搜尋以名稱優先排序", "test_search_ranks_name_matches_first",
                 "排序錯會讓模型選錯 skill。", "檢查 index.py catalog() 的加權。"),
            Item("中文查詢命中正確的 skill", "test_chinese_query_finds_the_right_skill",
                 "原本用 split() 加子字串比對，中文沒有空白，13 個真實查詢只命中 1 個（8%）。"
                 "改用 CJK bigram + BM25 後為 85%。",
                 "檢查 skill_server/search.py 的 tokenize() 與 SearchIndex.search()。"),
            Item("英文檢索未因中文支援而退化", "test_english_search_is_not_regressed",
                 "為一種語言優化不能犧牲另一種。", "檢查 tokenize() 的英文分支。"),
            Item("中英混合的說明兩種語言都可檢索",
                 "test_mixed_language_description_is_searchable_both_ways",
                 "內部 skill 的說明常常中英夾雜。", "檢查 _CJK_RE 的切分。"),
            Item("BM25 讓更專門的 skill 排前面", "test_bm25_ranks_the_more_specific_skill_first",
                 "兩個都提到同一個詞時，排序決定模型選哪個。", "檢查 IDF 與欄位權重。"),
            Item("新增 skill 後立刻可被檢索", "test_search_index_is_rebuilt_on_refresh",
                 "檢索索引沒跟著 refresh 重建，熱載入等於半殘。",
                 "確認 _Snapshot 的 search 欄位在 refresh() 時重建。"),
            Item("目錄放不下時改回傳領域總覽",
                 "test_overview_covers_every_area_instead_of_truncating",
                 "字典序截斷會讓整個領域對模型隱形，而它無從發現自己漏了什麼。"
                 "實測 315 個 skill 時有 15 個領域完全看不到。",
                 "檢查 server.py list_skills 的 overview 分支與 index.facets()。"),
            Item("小目錄仍回傳完整清單", "test_small_catalogue_still_returns_the_full_list",
                 "總覽只在放不下時啟用，不該影響一般情況。",
                 "檢查啟用條件是以完整目錄而非 limit 後的結果判斷。"),
            Item("標籤過濾需全部符合", "test_tag_filter_requires_all_tags",
                 "過濾語意錯會回傳不相干的 skill。", "檢查 catalog() 的 issubset 判斷。"),
            Item("內文快取且已去除 frontmatter", "test_body_is_cached_and_frontmatter_stripped",
                 "沒快取就每次讀磁碟；沒去 frontmatter 會浪費 token。",
                 "檢查 index.py body() 的 body_offset 與快取。"),
            Item("三種變動都偵測得到", "test_refresh_still_detects_content_and_structure_changes",
                 "背景輪詢預設關閉，但 reload_skills 與本機開發依賴這條路徑正確。",
                 "檢查 refresh() 的 stamps 比對。"),
            Item("改檔後快取會失效", "test_body_cache_invalidates_on_edit",
                 "快取不失效 = 改了 skill 卻沒生效。",
                 "檢查 (mtime_ns, size) 比對邏輯。"),
            Item("_shared/ 不會被當成 skill", "test_shared_directory_is_not_indexed_as_a_skill",
                 "共用程式碼要對模型完全不可見，否則重用就變成多付一次 load_skill。",
                 "確認 _discover() 只收有 SKILL.md 的目錄。"),
            Item("SKILL_ROOT 在任何深度都正確", "test_skill_root_resolves_shared_lib_at_any_depth",
                 "$SKILL_DIR/../ 在命名空間子目錄下會少算一層，source 直接失敗。",
                 "檢查 SkillMeta.root 與 runner 的 SKILL_ROOT 注入。"),
            Item("多個根目錄時 SKILL_ROOT 指向正確的那個",
                 "test_skill_root_is_exposed_and_points_at_the_owning_root",
                 "--skills 可指定多個根目錄。", "檢查 _discover() 回傳的 (path, root) 配對。"),
            Item("skill 名稱打錯會給建議", "test_unknown_skill_suggests_alternatives",
                 "讓模型自己修正，而不是卡住。", "檢查 index.py get() 的相似名稱提示。"),
        ],
    ),
    Group(
        title="安全",
        purpose="確認呼叫端（LLM）無法讀取或執行 skill 目錄以外的東西。這組失敗代表有實際風險，必須先修。",
        items=[
            Item("拒絕路徑穿越（../ 與絕對路徑）", "test_path_traversal_is_refused",
                 "否則可讀取 /etc/passwd 等任意檔案。",
                 "檢查 index.py resolve_file() 的 is_relative_to 判斷，不可放寬。"),
            Item("拒絕指向外部的 symlink", "test_symlink_out_of_bundle_is_refused",
                 "symlink 是繞過路徑檢查最常見的手法。",
                 "確認 resolve() 在包含性檢查之前呼叫。"),
            Item("只有 scripts/ 底下可執行", "test_only_scripts_dir_is_runnable",
                 "否則參考文件、設定檔都可能被當程式跑。",
                 "檢查 runner.py _build_argv() 的 SCRIPTS_SUBDIR 判斷。"),
            Item("未註冊副檔名不可執行", "test_unregistered_interpreter_is_refused",
                 "有執行位元不代表可以跑，白名單才算數。",
                 "檢查 runner.py INTERPRETERS，新增直譯器要是刻意決定。"),
            Item("伺服器的機密不會被 script 繼承", "test_server_secrets_are_not_inherited",
                 "否則 script 可以讀到伺服器環境裡的所有金鑰。",
                 "檢查 runner.py _child_env() 必須是白名單而非黑名單。"),
            Item("透過 MCP 工具的穿越也會被擋", "test_traversal_through_the_tool_is_an_error",
                 "確認防護在真實呼叫路徑上有效，不只在單元層。",
                 "檢查 server.py read_skill_file 有把 SkillLoadError 轉成 ToolError。"),
            Item("hook 的 symlink 逃逸會被擋", "test_hook_symlinked_outside_the_bundle_is_refused",
                 "曾經可行的攻擊：hooks/pre.py 指向外部會被執行，而且每次呼叫都執行。",
                 "檢查 runner.py run_path() 的 jail 參數與 is_relative_to 判斷。"),
            Item("呼叫端不能設定會改變執行對象的環境變數",
                 "test_caller_cannot_set_execution_changing_env",
                 "曾經可行的攻擊：env={'PATH': '/tmp/fake'} 等於遠端執行任意程式。"
                 "呼叫端是 LLM，可能被它讀到的文件說服。",
                 "檢查 runner.py 的 _CALLER_FORBIDDEN_ENV 與 _CALLER_FORBIDDEN_PREFIXES。"),
            Item("呼叫端不能劫持共用函式庫", "test_caller_cannot_hijack_the_shared_library_via_skill_root",
                 "曾經可行：env={'SKILL_ROOT': 攻擊者目錄} 能替換每支 script 都 source 的共用函式庫，"
                 "等同每次呼叫都執行任意程式。加入 SKILL_ROOT 時漏了放進封鎖清單。",
                 "確認 runner.py 的 _CALLER_FORBIDDEN_ENV 含 SKILL_ROOT。"),
            Item("正常的 env 用途仍然可行", "test_caller_can_still_pass_data_env",
                 "封鎖清單不能誤傷傳 token、base URL 這類正常需求。",
                 "檢查封鎖清單只含會改變「執行什麼」的變數。"),
            Item("stdin 有大小上限", "test_stdin_is_bounded",
                 "曾經可行：20MB stdin 全進記憶體。", "檢查 runner.py 的 MAX_STDIN_BYTES。"),
        ],
    ),
    Group(
        title="規格對齊",
        purpose="確認與 Claude Code 的 skill 規格一致：命名規則、description 上限、allowed-tools。",
        items=[
            Item("description 彼此分得開", "test_indistinguishable_descriptions_are_flagged",
                 "選錯 skill 是最常見的失敗模式，且上線後難以歸因——模型不會說它在猶豫。"
                 "單看每個 description 都合格，擺在一起才看得出問題。",
                 "跑 spec.validate --level=L2，依 LINT-040 的指示改寫 description。"),
            Item("寫得好的 description 不被誤報", "test_distinct_descriptions_pass_cleanly",
                 "誤報比漏報更傷（RFC-175），會訓練使用者忽略輸出。",
                 "檢查 DESCRIPTION_SIMILARITY_LIMIT 是否過低。"),
            Item("相似度門檻落在兩個分佈之間", "test_similarity_threshold_separates_the_two_populations",
                 "門檻若落在分佈內，不是好的被標記就是差的被放行。",
                 "重新校準 spec/validate.py 的 DESCRIPTION_SIMILARITY_LIMIT。"),
            Item("skill 名稱符合 Claude Code 規則", "test_skill_name_follows_claude_code_rules",
                 "小寫字母、數字、連字號，最多 64 字元。同時擋掉會讓 state 目錄逃逸的名稱。",
                 "把 SKILL.md 的 name 改成 kebab-case，例如 order-lookup。"),
            Item("過長的 description 會被拒絕並說明原因",
                 "test_oversized_description_is_rejected_with_a_reason",
                 "description 是唯一每次對話都載入的文字，上限 1024 字元。",
                 "把細節移到 skill 內文，description 只留路由判斷用的一句話。"),
            Item("allowed-tools 與 license 有解析並透傳",
                 "test_allowed_tools_is_parsed_and_passed_through",
                 "Claude Code 的欄位。本服務不強制執行但透傳給客戶端。",
                 "檢查 index.py 的 allowed-tools 解析。"),
            Item("hooks 不會被當成可讀內容列出", "test_hooks_are_not_listed_as_readable_content",
                 "hooks 是機制不是內容，列給模型看只是浪費 token。",
                 "檢查 _scan_bundle() 有跳過 hooks/ 目錄。"),
            Item("被拒絕的 skill 會出現在 stats", "test_skill_name_follows_claude_code_rules",
                 "skill 無聲消失是最難查的問題。",
                 "呼叫 skill_server_stats() 看 index.rejected。"),
        ],
    ),
    Group(
        title="卡住偵測",
        purpose="確認 script 卡在 API 呼叫時，能快速失敗並且告訴你卡在哪裡。",
        items=[
            Item("逾時仍保留已印出的內容", "test_output_survives_a_timeout",
                 "沒有這個，逾時只會回傳空字串，你不知道卡在哪一步。",
                 "檢查 runner.py 的 _Capture 必須由 runner 持有，不能放在 reader 協程裡。"),
            Item("靜默超過 stall_timeout 會提早砍掉", "test_stall_detection_kills_early",
                 "否則要等滿整個 timeout 才知道有問題。",
                 "檢查 runner.py _execute() 的 stall 判斷迴圈。"),
            Item("有心跳的慢 script 不會被誤砍", "test_progress_keeps_a_slow_script_alive",
                 "誤砍正常的長任務比不砍更糟。",
                 "確認 script 有定期輸出；這是 script 端的責任。"),
            Item("執行中即時串流輸出", "test_on_output_streams_lines_while_running",
                 "讓呼叫端看得到進度，不是黑箱等待。",
                 "檢查 runner.py _pump() 的 on_output 回呼。"),
            Item("真實卡住的 socket 會被抓到", "test_a_real_hanging_socket_is_caught",
                 "端對端驗證，不是模擬。",
                 "若失敗，先確認本機可以開 127.0.0.1 的臨時埠。"),
            Item("proxy / TLS 環境變數有傳給 script", "test_proxy_and_tls_env_reach_the_script",
                 "公司網路少了 HTTPS_PROXY，每個對外呼叫都會掛到逾時。",
                 "檢查 runner.py _NETWORK_ENV 清單。"),
            Item("可以關閉網路環境變數轉發", "test_network_env_can_be_switched_off",
                 "隔離環境需要能關。", "檢查 --no-network-env 參數。"),
            Item("逾時會殺掉整個行程群組", "test_timeout_kills_the_process",
                 "只殺子行程會留下孤兒。", "檢查 start_new_session=True 與 killpg。"),
            Item("輸出過量會截斷但不會卡死", "test_output_is_capped_without_deadlocking",
                 "停止讀取會讓子行程卡在滿的 pipe 上。",
                 "檢查 _Capture.feed()：超過上限仍要繼續讀，只是不再保留。"),
            Item("非零離開碼是回報而不是拋例外", "test_nonzero_exit_is_reported_not_raised",
                 "script 失敗是資料，不是伺服器錯誤。", "檢查 server.py 沒有對 exit_code 拋錯。"),
            Item("孫行程握住 pipe 時 timeout 仍然有效",
                 "test_timeout_holds_when_a_grandchild_keeps_the_pipe_open",
                 "曾經違約：script 0.05 秒就結束，但它 spawn 的孫行程繼承了 stdout，"
                 "pipe 沒有 EOF，runner 固定等 5 秒，timeout=2s 的呼叫實際跑了 5 秒。",
                 "檢查 runner.py finally 區塊的 grace 是否用 deadline 計算，而非固定值。"),
            Item("導開輸出的背景孫行程能活過 script",
                 "test_a_detached_background_child_survives_and_returns_immediately",
                 "這是「在 script 裡開背景工作」唯一可靠的寫法。",
                 "孫行程的 stdout/stderr 必須導向 DEVNULL 或檔案。"),
        ],
    ),
    Group(
        title="執行政策",
        purpose="確認 skill 能宣告每支 script 的 timeout，且服務不擅自解讀輸出的意義。（情境一）",
        items=[
            Item("frontmatter 的 execution 逐 script 解析", "test_execution_policy_is_parsed_per_script",
                 "同一個 skill 裡，送出型和等待型需要完全不同的 ceiling。",
                 "檢查 SKILL.md 的 execution 縮排是否正確。"),
            Item("輸出原樣回傳，服務不解讀", "test_output_is_returned_without_interpretation",
                 "script 印 uuid 或印資料，服務做的事完全相同：原樣回傳。"
                 "多一個 mode 旗標只是讓服務去猜它已經拿到的東西。",
                 "檢查 server.py 沒有依 mode 分支處理 stdout。"),
            Item("服務不會自己猜 job key", "test_server_never_guesses_a_job_key_from_ordinary_data",
                 "自動猜測會把訂單的 id、使用者的 id 誤判成任務代號。"
                 "錯的 handle 比沒有 handle 更糟，因為它看起來是對的。",
                 "確認 server.py 沒有 _extract_key 這類猜測函式。"),
            Item("短 ceiling 超時指向 async 邊界問題",
                 "test_short_ceiling_overrun_points_at_the_async_boundary",
                 "短 timeout 就是作者在說「這應該馬上回」，超時代表端點在回覆前先做完工作。",
                 "檢查 runner.py _diagnose() 的 limit <= 30 分支。"),
            Item("長短 ceiling 的診斷建議相反", "test_the_two_timeout_diagnoses_give_opposite_advice",
                 "兩者建議必須相反，否則會把人導到錯的方向。",
                 "檢查 runner.py _diagnose() 三個分支的文字。"),
            Item("宣告的 timeout 會自動生效", "test_declared_timeout_applies_without_the_caller_knowing",
                 "呼叫端不必知道細節就有正確預設。", "檢查 effective_timeout 的取用順序。"),
            Item("timeout 與說明會出現在卡片上", "test_timeout_is_visible_in_the_catalog",
                 "模型看得到每支 script 的 ceiling 與作者的自然語言說明。",
                 "檢查 index.py 的 ExecutionPolicy.as_card()。"),
        ],
    ),
    Group(
        title="Hooks",
        purpose="確認 pre / post 檢查機制真的能擋、能改寫、且沒用到的 skill 不會變慢。（情境二）",
        items=[
            Item("pre-hook 拒絕時 script 完全不執行", "test_pre_hook_can_deny_and_the_script_never_runs",
                 "只回報錯誤但仍執行，等於沒有防護。",
                 "檢查 server.py 是先跑 hook 再呼叫 runner.run。"),
            Item("pre-hook 可注入環境變數與改寫參數", "test_pre_hook_injects_env_and_rewrites_args",
                 "注入 request id、補預設參數的常見需求。", "檢查 hooks.py run_pre 的回傳處理。"),
            Item("post-hook 可改寫也可拒絕", "test_post_hook_can_rewrite_and_can_reject",
                 "輸出稽核與敏感資訊攔截。", "檢查 hooks.py run_post。"),
            Item("全域 hook 套用到每個 skill", "test_global_hooks_apply_to_every_skill",
                 "組織層級政策必須無法被單一 skill 繞過。", "檢查 --hooks-dir 參數與 _chain()。"),
            Item("沒有 hook 的 skill 不付額外成本", "test_skills_without_hooks_pay_nothing",
                 "hook 是 3 倍成本，不能讓所有人一起付。", "檢查 hooks.has_hooks() 的短路判斷。"),
        ],
    ),
    Group(
        title="熱載入",
        purpose="確認 skill 可在服務執行中載入，不需要重啟。（情境三）",
        items=[
            Item("執行中新增 skill 立即可用", "test_skills_load_at_runtime_without_a_restart",
                 "重啟才生效 = 每次改動都要中斷服務。", "檢查 reload_skills 工具與 index.refresh(force=True)。"),
            Item("改模式、加 hook 都免重啟", "test_edited_policy_and_new_hooks_take_effect_on_reload",
                 "行為的熱更新，不只是文字。", "確認 refresh 會重建 policies 與 hooks。"),
        ],
    ),
    Group(
        title="輸出預算",
        purpose="確認回傳內容會被壓到模型 context 裝得下的大小，且 JSON 不會被切壞。",
        items=[
            Item("預算隨 context 大小調整", "test_budget_scales_with_the_context_window",
                 "128k 的預設值會塞爆 30k 的地端模型。", "啟動時加 --context-tokens 30000。"),
            Item("JSON 用結構化縮減而非切半", "test_json_is_reduced_structurally_not_cut_in_half",
                 "切半的 JSON 無法解析，等於白花 token。", "檢查 shaping.py _reduce_json。"),
            Item("小輸出原樣通過", "test_small_output_is_passed_through_untouched",
                 "不該對正常大小的輸出動手腳。", "檢查 shape() 的提前返回。"),
            Item("非 JSON 保留頭尾", "test_non_json_keeps_head_and_tail",
                 "錯誤訊息通常在結尾，只留開頭會丟掉重點。", "檢查 _shape_text。"),
            Item("超寬資料退回結構描述", "test_wide_rows_fall_back_to_an_outline_that_still_parses",
                 "極端情況也要保持合法 JSON。", "檢查 _outline。"),
            Item("工具層實際套用預算", "test_tool_shapes_output_to_the_context_budget",
                 "端對端驗證，不只單元層。", "檢查 server.py 是否呼叫 shaping.shape。"),
            Item("參考檔也受預算限制", "test_reference_files_respect_the_context_budget",
                 "曾經的缺口：400KB 參考檔（約 114k tokens）直接回傳，"
                 "而且外觀是「讀取成功」不是錯誤。",
                 "檢查 server.py read_skill_file 有呼叫 shaping.shape。"),
            Item("超大 skill 內文也受預算限制", "test_oversized_skill_body_respects_the_budget",
                 "同一個缺口也存在於 load_skill。",
                 "檢查 server.py load_skill 有呼叫 shaping.shape。"),
            Item("skill 目錄本身受 context 預算限制", "test_catalog_respects_the_context_budget",
                 "曾經的缺口：500 個 skill 的目錄約 23k tokens，等於 30k 視窗的 78%，"
                 "而 list_skills 是唯一沒套預算的工具。",
                 "檢查 server.py list_skills 的 catalog_budget_bytes 裁切。"),
            Item("script 知道自己的輸出預算", "test_scripts_are_told_the_budget",
                 "在來源就過濾，比事後縮減準確。", "檢查 SKILL_OUTPUT_BUDGET_BYTES 環境變數。"),
        ],
    ),
    Group(
        title="背景任務",
        purpose="確認 fire-and-forget 的 key 不會遺失，以及輪詢腳本的心跳規則。",
        items=[
            Item("有心跳的輪詢不會被誤砍", "test_heartbeat_saves_a_poller_from_stall_detection",
                 "同時驗證「安靜的輪詢一定要被砍」這個相對的規則。",
                 "輪詢腳本必須定期輸出，間隔要小於 stall_timeout。"),
            Item("被砍掉也拿得到已送出的 uuid", "test_a_killed_submit_still_yields_the_job_id",
                 "uuid 遺失 = 任務跑完但沒人能取結果。",
                 "submit 腳本必須「先印 uuid，再做其他事」。"),
            Item("await 有上限，未完成也算成功", "test_await_returns_bounded_and_not_finished_is_success",
                 "避免任何一次呼叫無限等待。", "檢查 job.py 的 --max-wait 邏輯。"),
            Item("服務不提供任何可寫目錄", "test_no_state_dir_by_default",
                 "唯讀根檔案系統下曾經每一支 script 都失敗。功能已完全移除。",
                 "確認 runner/server 裡沒有任何狀態目錄邏輯。"),
            Item("可寫狀態的 API 完全不存在", "test_no_writable_state_api_exists_at_all",
                 "「不寫檔案」是契約不是預設值。有人想加回來會先撞到這條。",
                 "移除新加的 state_root / SKILL_STATE_DIR。"),
            Item("服務執行期間不建立任何檔案", "test_server_creates_no_files_while_serving",
                 "端對端保證，讓 readOnlyRootFilesystem 成立。",
                 "找出是誰在寫檔案。"),
            Item("行程結束就返回，沒有多餘等待",
                 "test_runner_returns_as_soon_as_the_process_exits",
                 "等的是行程結束不是 stdout 關閉——script 印完留尾巴會拖住整個呼叫。",
                 "檢查 runner._execute 的等待邏輯。"),
            Item("api-call 是最快路徑", "test_api_call_skill_is_the_fast_path",
                 "bash+curl 比 Python 快 21 倍，在 0.1 core 上差距被放大。",
                 "檢查 skills/api-call/scripts/call.sh。"),
            Item("api-call 的錯誤回應是合法 JSON",
                 "test_api_call_reports_http_errors_without_raising",
                 "壞掉的 JSON 讓模型看不懂還照燒 token。",
                 "檢查 call.sh 的 json_string() 轉義。"),
        ],
    ),
    Group(
        title="規範符合性",
        purpose="確認所有 Skill 通過 RFC-SKILL-1 的機器驗證。這組是規範的可執行形式。",
        items=[
            Item("所有 skill 通過 L1 結構驗證", "test_all_skills_pass_spec_l1",
                 "L1 涵蓋所有 MUST 規則；未通過代表 Skill 不會被載入或有安全問題。",
                 "執行 uv run python -m spec.validate skills/ 依指示修正。"),
            Item("驗證器能偵測安全違規", "test_validator_detects_security_violations",
                 "對應附錄 A.1 的實際攻擊：symlink 逃逸、scripts 外的可執行檔、無 timeout。",
                 "檢查 spec/validate.py 的 check_filesystem 與 check_scripts。"),
            Item("豁免必須附理由且可見", "test_suppression_requires_a_reason",
                 "沒有理由的豁免等於關閉規則；靜默的豁免無法審查。",
                 "檢查 SUPPRESS_RE 與 LINT-030 的產生。"),
            Item("schema 皆為合法 Draft 2020-12", "test_every_schema_is_valid_json",
                 "schema 是規範的機器可讀形式，壞掉等於規範失效。",
                 "以 python -c 'import json' 逐一檢查 spec/schemas/*.json。"),
            Item("驗證器能偵測已知違規", "test_validator_detects_known_violations",
                 "只會通過的驗證器沒有價值。這條確保偵測邏輯真的有效。",
                 "檢查 spec/validate.py 的偵測函式。"),
            Item("驗證報告符合自身 schema", "test_validation_report_matches_schema",
                 "報告是 CI 的介面，格式錯誤會讓自動化失效。",
                 "比對 spec/schemas/validation-report.schema.json。"),
        ],
    ),
    Group(
        title="相容性",
        purpose="確認破壞性變更不會靜默發生。這組失敗代表呼叫端可能在下次部署後壞掉。",
        items=[
            Item("每類變更都分類正確", "test_compat_classifies_every_change_type",
                 "分類錯會讓真正的破壞性變更被當成 minor 放行。",
                 "檢查 spec/compat.py 的 compare() 與 RFC-08 §14.2 對照。"),
            Item("破壞性變更沒 bump major 會失敗",
                 "test_compat_fails_when_breaking_change_lacks_major_bump",
                 "破壞相容性卻沒標記，是最糟的失敗模式——呼叫端突然壞掉且無跡可循。",
                 "提升該 skill 的 major 版本，或撤銷破壞性變更。"),
            Item("正確 bump 後放行", "test_compat_passes_when_major_is_bumped",
                 "工具不該阻止刻意的破壞性變更，只堅持它被標記。",
                 "檢查 check_versions() 的 major 比較。"),
            Item("相容的變更不被誤判", "test_compat_treats_minor_changes_as_compatible",
                 "放寬 timeout、新增 tag 都不是破壞性的，誤判會擋住正常開發。",
                 "檢查 compare() 的 minor 分支。"),
            Item("base 版本不 checkout 到磁碟", "test_compat_reads_base_from_git_without_checkout",
                 "工具與服務一樣不寫檔案。", "確認使用 git show 而非 git checkout。"),
            Item("與自己比對為零差異", "test_compat_reports_no_diff_against_itself",
                 "有差異代表讀取路徑有 bug。", "檢查 read_manifests_at 與 from_disk 的一致性。"),
        ],
    ),
    Group(
        title="維運",
        purpose="確認 k8s 探針、指標與關機流程可用。這組失敗會導致 pod 起不來或滾動更新掉資料。",
        items=[
            Item("/health 與 /ready 可用純 HTTP 存取",
                 "test_health_and_ready_endpoints_answer_plain_http",
                 "k8s probe 不會說 JSON-RPC。GET /mcp 回 405/406，指向它會讓 pod 永遠 NotReady。",
                 "確認 server.py 有 @mcp.custom_route 的 /health 與 /ready。"),
            Item("沒有 skill 時 /ready 回 503",
                 "test_ready_reports_503_when_no_skills_loaded",
                 "空索引通常是 --skills 路徑設錯。接流量比不接更糟：模型會認定工具不存在。",
                 "檢查 /ready 的 status_code 判斷。"),
            Item("/metrics 是 Prometheus 格式", "test_metrics_endpoint_is_prometheus_readable",
                 "沒有指標就無法告警。", "檢查 server.py 的 /metrics 路由。"),
            Item("關機時等待執行中的 script", "test_drain_waits_for_in_flight_scripts",
                 "滾動更新硬砍會讓已送出、還沒印出 uuid 的任務失去 handle。",
                 "檢查 runner.drain() 與 lifespan 的 finally。"),
            Item("drain 有上限且如實回報", "test_drain_is_bounded_and_reports_abandoned",
                 "k8s 只給 terminationGracePeriodSeconds，不能無限等。",
                 "檢查 drain() 的 deadline 邏輯。"),
        ],
    ),
    Group(
        title="MCP 介面",
        purpose="確認對外工具、資源與 CLI 參數都正確接上。",
        items=[
            Item("工具清單符合預期", "test_tools_are_exposed",
                 "少一個或多一個工具都代表接線有問題。", "檢查 server.py 的 @mcp.tool 裝飾器。"),
            Item("列出→載入→執行 完整流程", "test_list_then_load_then_run",
                 "最主要的使用路徑。", "依錯誤訊息逐段檢查。"),
            Item("stdin 有正確傳入", "test_stdin_is_piped_through",
                 "大量文字用 stdin 傳，不要塞進 argv。", "檢查 runner 的 stdin 處理。"),
            Item("可讀取 skill 附帶的參考檔", "test_reference_file_is_readable",
                 "漸進式揭露的第三層。", "檢查 read_skill_file。"),
            Item("resource 可取得 skill 內文", "test_resource_serves_the_body",
                 "給偏好 resource 的客戶端使用。", "檢查 @mcp.resource 註冊。"),
            Item("統計含延遲百分位", "test_stats_reports_latency",
                 "沒有數據就無法判斷效能問題。", "檢查 TimingMiddleware。"),
            Item("shell script 在有歷史的 repo 可執行", "test_shell_skill_succeeds_on_a_repo_with_history",
                 "驗證非 Python 腳本路徑。", "確認本機有 git。"),
            Item("shell script 處理空 repo", "test_shell_skill_handles_a_repo_with_no_commits",
                 "邊界情況。", "檢查 digest.sh 的空 repo 判斷。"),
            Item("每個 CLI 參數都有定義", "test_every_cli_flag_reaches_build_server",
                 "曾經發生過參數漏掉導致服務起不來。", "在 parse_args() 補上缺少的 add_argument。"),
            Item("CLI 參數解析正確", "test_cli_parses_the_documented_flags",
                 "文件寫的參數必須真的存在。", "比對 README 的參數列表。"),
            Item("stdin 的機密不進 argv", "test_per_run_env_carries_secrets_out_of_argv",
                 "argv 會出現在行程列表與日誌裡。", "用工具的 env 參數傳機密。"),
        ],
    ),
]


# ------------------------------------------------------------------ 輔助輸出

class C:
    """終端顏色。不支援時自動退成空字串。"""

    on = sys.stdout.isatty()
    GREEN = "\033[32m" if on else ""
    RED = "\033[31m" if on else ""
    YELLOW = "\033[33m" if on else ""
    DIM = "\033[2m" if on else ""
    BOLD = "\033[1m" if on else ""
    OFF = "\033[0m" if on else ""


def title(text: str) -> None:
    print(f"\n{C.BOLD}{text}{C.OFF}")
    print("─" * 60)


# ------------------------------------------------------------------ 前置檢查

def preflight() -> list[str]:
    """跑測試之前先確認環境。環境壞掉時的錯誤訊息會很難懂，先擋下來。"""
    problems = []
    if sys.version_info < (3, 12):
        problems.append(
            f"Python 版本是 {sys.version_info.major}.{sys.version_info.minor}，需要 3.12 以上。"
            "\n      解法：uv sync（uv 會自動裝好對的版本）"
        )
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        problems.append("找不到 fastmcp 套件。\n      解法：在專案目錄執行 uv sync")
    if not (ROOT / "skills").is_dir():
        problems.append(f"找不到 skills 目錄：{ROOT / 'skills'}\n      解法：確認在專案根目錄執行")
    if shutil.which("git") is None:
        problems.append("找不到 git 指令，兩項 shell script 測試會失敗。\n      解法：安裝 git")
    return problems


# ------------------------------------------------------------------ 執行測試

def run_group(group: Group, verbose: bool) -> tuple[int, int, list[tuple[Item, str]]]:
    """執行一組，回傳 (通過數, 失敗數, 失敗清單)。"""
    title(f"【{group.title}】{group.purpose}")

    selector = " or ".join(item.selector for item in group.items)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "-k", selector, "tests/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    failed_names = set(re.findall(r"FAILED tests/\S+::(\w+)", output))
    errored = set(re.findall(r"ERROR tests/\S+::(\w+)", output))
    broken = failed_names | errored

    passed = failures = 0
    problems: list[tuple[Item, str]] = []
    for item in group.items:
        # selector 是子字串比對，參數化測試會展開成多筆
        hit = [name for name in broken if item.selector in name or name in item.selector]
        if hit:
            failures += 1
            print(f"  {C.RED}[失敗]{C.OFF} {item.name}")
            print(f"         為什麼重要：{item.why}")
            print(f"         {C.YELLOW}下一步：{item.on_fail}{C.OFF}")
            problems.append((item, output))
        else:
            passed += 1
            print(f"  {C.GREEN}[通過]{C.OFF} {item.name}")
            if verbose:
                print(f"         {C.DIM}{item.why}{C.OFF}")
    return passed, failures, problems


def smoke() -> bool:
    """冒煙測試：真的把服務啟起來，確認端點會回應。"""
    title("【冒煙測試】實際啟動服務並確認可連線")
    port = 8791
    proc = subprocess.Popen(
        [sys.executable, "-m", "skill_server.server", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        import urllib.error
        import urllib.request

        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print(f"  {C.RED}[失敗]{C.OFF} 服務啟動就結束了")
                print(f"         {C.YELLOW}下一步：看下面的錯誤訊息，通常是 CLI 參數或 skill 格式問題{C.OFF}")
                print("         " + "\n         ".join(out.strip().splitlines()[-12:]))
                return False
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=1)
                break
            except urllib.error.HTTPError:
                break  # 有回應就算活著（MCP 端點對 GET 會回 4xx）
            except Exception:
                time.sleep(0.5)
        else:
            print(f"  {C.RED}[失敗]{C.OFF} 等 30 秒仍無法連上 127.0.0.1:{port}")
            print(f"         {C.YELLOW}下一步：確認該埠沒被占用，或改用其他埠測試{C.OFF}")
            return False

        print(f"  {C.GREEN}[通過]{C.OFF} 服務在 127.0.0.1:{port}/mcp 正常回應")
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="skill-mcp 驗收測試")
    parser.add_argument("--group", action="append", help="只跑指定組別（可重複）")
    parser.add_argument("--list", action="store_true", help="列出所有組別後結束")
    parser.add_argument("--smoke", action="store_true", help="只做啟動冒煙測試")
    parser.add_argument("--verbose", action="store_true", help="通過的項目也印出說明")
    args = parser.parse_args()

    if args.list:
        print("可用組別：\n")
        for group in GROUPS:
            print(f"  {group.title:<10} {len(group.items):>2} 項  {group.purpose}")
        print(f"\n共 {sum(len(g.items) for g in GROUPS)} 項")
        return 0

    print(f"{C.BOLD}skill-mcp 驗收測試{C.OFF}")
    print(f"專案目錄：{ROOT}")

    title("【環境檢查】確認可以開始測試")
    if problems := preflight():
        for problem in problems:
            print(f"  {C.RED}[環境問題]{C.OFF} {problem}")
        print(f"\n{C.RED}環境還沒準備好，測試沒有開始。先解決上面的問題。{C.OFF}")
        return 2
    print(f"  {C.GREEN}[通過]{C.OFF} Python、fastmcp、skills 目錄、git 都就緒")

    if args.smoke:
        return 0 if smoke() else 1

    groups = GROUPS
    if args.group:
        wanted = {name.lower() for name in args.group}
        groups = [g for g in GROUPS if g.title.lower() in wanted]
        if not groups:
            print(f"\n{C.RED}找不到組別 {args.group}。用 --list 看可用的組別。{C.OFF}")
            return 2

    total_pass = total_fail = 0
    first_failure: tuple[Item, str] | None = None
    for group in groups:
        passed, failed, problems = run_group(group, args.verbose)
        total_pass += passed
        total_fail += failed
        if problems and first_failure is None:
            first_failure = problems[0]

    ok = smoke() if not args.group else True

    title("【總結】")
    print(f"  通過 {total_pass} 項，失敗 {total_fail} 項")
    if total_fail == 0 and ok:
        print(f"\n{C.GREEN}{C.BOLD}全部通過，可以部署。{C.OFF}")
        return 0

    print(f"\n{C.RED}{C.BOLD}有項目失敗。請只處理第一項，修好後重跑這支程式。{C.OFF}")
    if first_failure:
        item, output = first_failure
        print(f"\n第一個要修的是：{C.BOLD}{item.name}{C.OFF}")
        print(f"  下一步：{item.on_fail}")
        print(f"  重跑指令：uv run python acceptance.py --group <組別名>")
        detail = [line for line in output.splitlines() if line.startswith("E ")][:8]
        if detail:
            print(f"\n{C.DIM}原始錯誤（供參考，不需要完全看懂）：{C.OFF}")
            for line in detail:
                print(f"  {C.DIM}{line}{C.OFF}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
