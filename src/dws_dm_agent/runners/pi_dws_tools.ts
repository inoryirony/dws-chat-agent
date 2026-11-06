/**
 * dws 服务工具扩展（front 与 worker 都挂载）：
 *   - no_reply：零参数，表示"当前输入已处理，本次不向联系人发送消息"。
 *     terminate 结束本轮；真正的 silent 结果由服务在 tool_execution_end 捕获。
 *   - task_record / task_lookup：事项记忆读写，按次拉起 task_tools.py
 *     （复用审计 SQLite），作用域与运行信息经 DWS_TASK_CONTEXT 注入。
 *   - report(progress|final|blocked)：progress 落进度流水不结束会话；
 *     final/blocked 把正文写入 DWS_REPORT_FILE 并 terminate，runtime 在
 *     tool_execution_end 读文件收口（与 no_reply 的 silent 同一通路）。
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { writeFileSync } from "node:fs";

/** 单次 task 工具调用的兜底超时（毫秒），避免 SQLite 挂起拖住整轮。 */
const TASK_TOOL_TIMEOUT_MS = 20_000;

function textResult(value: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(value) }],
		details: {},
	};
}

async function callTaskTool(request: unknown) {
	const script = process.env.DWS_TASK_SCRIPT;
	const context = process.env.DWS_TASK_CONTEXT;
	if (!script || !context) {
		throw new Error("dws task tools are not configured for this session");
	}
	const child = spawn(
		process.env.DWS_TASK_PYTHON || "python3",
		[script],
		{ stdio: ["pipe", "pipe", "pipe"] },
	);
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<unknown>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("dws task tool timed out"));
		}, TASK_TOOL_TIMEOUT_MS);
		lines.on("line", (line) => {
			clearTimeout(timer);
			child.kill();
			try {
				resolve(JSON.parse(line));
			} catch (error) {
				reject(new Error(`dws task tool returned invalid JSON: ${error}`));
			}
		});
		child.on("error", reject);
		child.stderr.on("data", () => {
			// 后端始终以 stdout JSON 表达结果，stderr 只作排障，不参与协议。
		});
		write(request);
	});
}

const noReply = defineTool({
	name: "no_reply",
	label: "No reply",
	description:
		"End this run without sending any message to the contact. " +
		"Call it when the current input needs no new reply: no new result, question, " +
		"risk, confirmation, or promised follow-up work remains to deliver. " +
		"It takes no arguments; the run ends silently.",
	parameters: Type.Object({}, { additionalProperties: false }),
	async execute() {
		return {
			content: [
				{
					type: "text" as const,
					text: "本次不发送消息，处理已静默结束。",
				},
			],
			details: {},
			terminate: true,
		};
	},
});

const taskRecord = defineTool({
	name: "task_record",
	label: "Record task",
	description:
		"Create or update a task in the shared task memory for this contact. " +
		"Create: omit task_id and provide goal (status defaults to open). " +
		"Update: provide task_id and its current expected_version; only submit " +
		"changed fields, omitted fields keep their current values (status never " +
		"falls back to open on update). Statuses: open (work remains), waiting " +
		"(waiting for someone; set waiting_on), completed (evidence the goal was " +
		"met), cancelled (evidence it was cancelled or superseded). Setting a " +
		"status other than waiting clears waiting_on unless you submit a new " +
		"value; submit waiting_on: \"\" to clear it explicitly. Resubmitting " +
		"identical content changes nothing. Restoring an archived task requires " +
		"restore=true together with new evidence.",
	parameters: Type.Object(
		{
			task_id: Type.Optional(
				Type.String({ description: "Existing task id; omit to create" }),
			),
			goal: Type.Optional(
				Type.String({ description: "Judgable goal of the task" }),
			),
			status: Type.Optional(
				Type.Union([
					Type.Literal("open"),
					Type.Literal("waiting"),
					Type.Literal("completed"),
					Type.Literal("cancelled"),
				]),
			),
			ask: Type.Optional(
				Type.String({
					description:
						"When status=waiting and waiting_on is the operator: " +
						"one line stating what decision you need from them. " +
						"goal says what the problem is; ask says what they must " +
						"decide. Pass \"\" to clear.",
				}),
			),
			waiting_on: Type.Optional(
				Type.String({
					description:
						"What is being waited for, when waiting; pass \"\" to clear",
				}),
			),
			evidence_summary: Type.Optional(
				Type.String({ description: "Last observed fact; never speculation" }),
			),
			evidence_message_ids: Type.Optional(
				Type.Array(Type.String(), {
					description:
						"Message ids supporting this change; only ids from " +
						"the current run's covered messages (current input " +
						"plus supplements steered into this run); unknown " +
						"ids are rejected",
				}),
			),
			resources: Type.Optional(
				Type.Array(Type.String(), {
					description:
						"Resource tags this task touches (e.g. \"repo:lucid\", " +
						"\"tool:lxs-report\"); replaces the whole list",
				}),
			),
			facts: Type.Optional(
				Type.Array(
					Type.Object({
						text: Type.String({
							description: "Durable fact worth remembering across runs",
						}),
						anchor_message_id: Type.String({
							description:
								"Id of the covered message this fact is anchored " +
								"to; unknown ids are rejected",
						}),
					}),
					{ description: "Append-only task facts (long-term memory)" },
				),
			),
			expected_version: Type.Optional(
				Type.Integer({
					description: "Current version of the task; required for updates",
				}),
			),
			restore: Type.Optional(
				Type.Boolean({ description: "Restore an archived task with new evidence" }),
			),
		},
		{ additionalProperties: false },
	),
	async execute(toolCallId: string, params: Record<string, unknown>) {
		return textResult(
			await callTaskTool({
				tool: "task_record",
				tool_call_id: toolCallId,
				args: params,
			}),
		);
	},
});

const taskLookup = defineTool({
	name: "task_lookup",
	label: "Read the board",
	description:
		"Read the task board across contacts: open/waiting tasks by default, " +
		"completed/cancelled with include_done=true (for later follow-ups), " +
		"archived tasks with include_archived=true (needed to restore one). " +
		"Filter by a contact_id from a previous result to see one contact's " +
		"tasks. Each task carries its latest facts; looking up does not " +
		"extend a task's lifetime.",
	parameters: Type.Object(
		{
			contact: Type.Optional(
				Type.String({
					description: "Only tasks touched by this contact_id",
				}),
			),
			status: Type.Optional(
				Type.Union([
					Type.Literal("open"),
					Type.Literal("waiting"),
					Type.Literal("completed"),
					Type.Literal("cancelled"),
				]),
			),
			include_done: Type.Optional(
				Type.Boolean({
					description:
						"Include completed/cancelled tasks; default false",
				}),
			),
			include_archived: Type.Optional(
				Type.Boolean({
					description:
						"Include archived tasks; default false. Needed to read " +
						"an archived task's id/version before restore=true",
				}),
			),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })),
		},
		{ additionalProperties: false },
	),
	async execute(toolCallId: string, params: Record<string, unknown>) {
		return textResult(
			await callTaskTool({
				tool: "task_lookup",
				tool_call_id: toolCallId,
				args: params,
			}),
		);
	},
});

const report = defineTool({
	name: "report",
	label: "Report progress or finish",
	description:
		"kind=progress: report a short milestone (one line, human-readable); " +
		"the run continues. kind=final: deliver text as the final answer to " +
		"the contact and end the run. kind=blocked: deliver text and end the " +
		"run as blocked (cannot proceed without something external). " +
		"After final/blocked the run ends immediately; write nothing else.",
	parameters: Type.Object(
		{
			kind: Type.Union([
				Type.Literal("progress"),
				Type.Literal("final"),
				Type.Literal("blocked"),
			]),
			text: Type.String({
				description:
					"progress: one-line milestone; final/blocked: the message " +
					"the contact will receive",
			}),
		},
		{ additionalProperties: false },
	),
	async execute(toolCallId: string, params: Record<string, unknown>) {
		const kind = String(params.kind || "");
		const text = String(params.text || "").trim();
		if (!text) {
			return textResult({ ok: false, error: "report_text_required" });
		}
		if (kind === "progress") {
			return textResult(
				await callTaskTool({
					tool: "report",
					tool_call_id: toolCallId,
					args: { kind, text },
				}),
			);
		}
		const reportFile = process.env.DWS_REPORT_FILE;
		if (!reportFile) {
			throw new Error("dws report tool is not configured for this session");
		}
		writeFileSync(reportFile, JSON.stringify({ kind, text }), "utf-8");
		return {
			content: [
				{
					type: "text" as const,
					text:
						kind === "final"
							? "最终正文已登记，本次运行结束。"
							: "已登记阻塞说明，本次运行结束。",
				},
			],
			details: {},
			terminate: true,
		};
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(noReply);
	pi.registerTool(taskRecord);
	pi.registerTool(taskLookup);
	pi.registerTool(report);
}
