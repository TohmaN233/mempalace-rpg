// 自动归档 RPG 场景到外部 Memo Palace。
//
// 设计目标：
// - 每个“入戏/剧本”回合都保存完整 transcript 到 scene_record + palace drawers。
// - 同时提交一个保守的 scene_transcript 事件，触发 kernel 的 memory_item / actor_belief
//   / world_fact 投影管道。
// - 明确排除工程维护、工具讨论、状态调试、GM/玩家 meta 交流，避免污染角色记忆。
//
// 注意：mempalace 当前 MVP 尚未实现 LLM event extractor。这里的自动化是宿主层
// deterministic extractor：只生成“本回合原文已发生/被见证”的保守事件。承诺、秘密、
// 阵营变化等高价值结构化事件仍可由 GM 额外调用 mcp_rpg_commit_scene 精细写入。

import type { ToolResultMessage } from "@earendil-works/pi-ai";

type AgentMessage = any;
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { getState } from "../core/state.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(__dirname, "..", "..");

const CAMPAIGN_ID = "fated-poem-dusk-song";

const META_PATTERNS = [
  /\b(package|npm|git|github|jsdelivr|typescript|typecheck|debug|schema|state|patch|json|api|mcp|subagent|memo\s*palace)\b/i,
  /脚本|代码|文件|配置|调试|发布|打包|修复|报错|工具|指令|变量|状态|面板|系统提示词|提示词|记忆宫殿|记忆库|自动化|功能漏了|加一个|规则改为|存档|经验同步|工程|实现|修改/,
  /查看.*状态|列出.*指令|有哪些.*功能|怎么.*写入|如何.*自动/,
];

const NARRATIVE_HINTS = [
  /「[^」]{2,}」/,
  /她|他|晓玥|冷月|遥娜|妲丽安|塞拉菲娜|赫利俄丝|伊兰维尔/,
  /走|看|听|握|抬头|低声|黄昏|夜|风|雪|血|剑|门|街|学院|石屋|森林|战斗|承诺|告白|威胁/,
];

let knownCharacterNames: string[] | null = null;
let characterIdByName: Map<string, string> | null = null;

function textFromContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    const raw = block as Record<string, unknown>;
    // 只归档真正展示给玩家的文字；thinking/toolCall/image 等都不是剧本文本。
    if (raw.type === "text" && typeof raw.text === "string") parts.push(raw.text);
  }
  return parts.join("\n").trim();
}

export function extractVisibleText(message: AgentMessage | undefined): string {
  if (!message) return "";
  return textFromContent((message as any).content).trim();
}

function normalizeForScore(text: string): string {
  return text.replace(/```[\s\S]*?```/g, " ").replace(/\s+/g, " ").trim();
}

function isLikelyMetaTurn(userText: string, assistantText: string): boolean {
  const joined = normalizeForScore(`${userText}\n${assistantText}`);
  if (!joined) return true;

  const metaScore = META_PATTERNS.reduce((n, re) => n + (re.test(joined) ? 1 : 0), 0);
  const narrativeScore = NARRATIVE_HINTS.reduce((n, re) => n + (re.test(assistantText) ? 1 : 0), 0);

  // 明显工程/规则/工具交流直接排除。若同时有较强叙事迹象，允许归档，避免误杀。
  if (metaScore >= 2 && narrativeScore < 2) return true;
  if (metaScore >= 1 && narrativeScore === 0) return true;

  // 表格式/清单式答复大多是 GM meta，而不是剧本。
  const bulletLines = assistantText.split("\n").filter((line) => /^\s*(?:[-*]|\d+[.)]|#{1,4}\s|\|)/.test(line)).length;
  if (bulletLines >= 5 && narrativeScore < 2) return true;

  return false;
}

function loadCharacterEntityMap(): Map<string, string> {
  if (characterIdByName) return characterIdByName;
  const map = new Map<string, string>();
  try {
    const path = join(projectRoot, "data", "memory_npc_overrides.json");
    if (existsSync(path)) {
      const data = JSON.parse(readFileSync(path, "utf-8"));
      const characterBlocks = { ...(data?.characters || {}), ...(data?.character_overrides || {}) };
      for (const [name, value] of Object.entries(characterBlocks)) {
        const entityId = (value as Record<string, unknown>)?._entity_id;
        if (typeof name === "string" && typeof entityId === "string") map.set(name, entityId);
      }
    }
  } catch {
    // ignore
  }
  characterIdByName = map;
  return characterIdByName;
}

function readKnownCharacterNames(): string[] {
  if (knownCharacterNames) return knownCharacterNames;
  const names = new Set<string>();
  const map = loadCharacterEntityMap();
  for (const name of map.keys()) if (name.length >= 2) names.add(name);
  try {
    const path = join(projectRoot, "data", "memory_npc_overrides.json");
    if (existsSync(path)) {
      const data = JSON.parse(readFileSync(path, "utf-8"));
      for (const name of data?._meta?.characters_list || []) {
        if (typeof name === "string" && name.length >= 2) names.add(name);
      }
    }
  } catch {
    // ignore
  }
  knownCharacterNames = [...names].sort((a, b) => b.length - a.length);
  return knownCharacterNames;
}

function entityIdForName(name: string): string {
  return loadCharacterEntityMap().get(name) || name;
}

function uniq(values: Array<string | undefined | null>): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of values) {
    const value = String(raw || "").trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

function collectPresentParticipants(state: Record<string, unknown>): string[] {
  const pc = state.主角 as Record<string, unknown> | undefined;
  const names: string[] = [entityIdForName(String(pc?.姓名 || "player"))];

  const relations = (state.关系列表 as Record<string, any> | undefined) || {};
  for (const [key, value] of Object.entries(relations)) {
    if (value && typeof value === "object" && (value.在场 === true || value.present === true || value.参战中 === true)) {
      names.push(entityIdForName(String(value.姓名 || value.名称 || key)));
    }
  }

  const battleNpcs = ((state.战斗 as Record<string, any> | undefined)?.NPC || {}) as Record<string, any>;
  for (const [key, value] of Object.entries(battleNpcs)) {
    if (!value || typeof value !== "object" || value.参战中 !== false) names.push(entityIdForName(String(value?.姓名 || key)));
  }

  return uniq(names);
}

function collectMentionedKnownEntities(transcript: string): string[] {
  return readKnownCharacterNames()
    .filter((name) => transcript.includes(name))
    .slice(0, 24)
    .map(entityIdForName);
}

function summarizeForEvent(assistantText: string, userText: string): string {
  const base = assistantText || userText;
  const compact = base.replace(/\s+/g, " ").trim();
  if (compact.length <= 220) return compact || "自动归档本回合场景。";
  return compact.slice(0, 219).trim() + "…";
}

function findMempalaceCli(): string | null {
  const candidates = [
    process.env.MEMPALACE_RPG_CLI,
    "mempalace-rpg",
    join(projectRoot, "..", "mempalace", ".venv", "bin", "mempalace-rpg"),
    "/home/tgy23/PI_workingspace/mempalace/.venv/bin/mempalace-rpg",
  ].filter(Boolean) as string[];

  for (const candidate of candidates) {
    if (candidate.includes("/") && existsSync(candidate)) return candidate;
    try {
      execFileSync("bash", ["-lc", `command -v ${JSON.stringify(candidate)}`], { stdio: "pipe" });
      return candidate;
    } catch {
      // try next
    }
  }
  return null;
}

function commitPayload(payload: Record<string, unknown>) {
  const cli = findMempalaceCli();
  if (!cli) return { ok: false, reason: "mempalace-rpg CLI not found" };

  const dir = mkdtempSync(join(tmpdir(), "dest-poet-memo-"));
  const file = join(dir, "scene.json");
  writeFileSync(file, JSON.stringify(payload, null, 2), "utf-8");
  try {
    execFileSync(cli, [
      "--db", join(projectRoot, "state", "rpg-memory.sqlite3"),
      "--palace", join(projectRoot, "state", "rpg-palace"),
      "--memo-setting", join(projectRoot, "memo_setting.json"),
      "commit-scene", file,
    ], { cwd: projectRoot, stdio: "pipe" });
    return { ok: true };
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error);
    // deterministic scene_id 可能在分支回放/重试时重复；这不是错误。
    if (/UNIQUE constraint failed|IntegrityError|already exists/i.test(text)) return { ok: true, duplicate: true };
    return { ok: false, reason: text };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

export function buildAutoCommitPayload(options: {
  userText: string;
  assistantMessage: AgentMessage | undefined;
  toolResults?: ToolResultMessage[];
}): { skip: true; reason: string } | { skip: false; payload: Record<string, unknown> } {
  const assistantText = extractVisibleText(options.assistantMessage);
  const userText = options.userText.trim();

  if (!assistantText || !userText) return { skip: true, reason: "empty visible transcript" };
  if (isLikelyMetaTurn(userText, assistantText)) return { skip: true, reason: "meta/gm/tool conversation" };

  const state = getState();
  const world = (state.世界 as Record<string, unknown> | undefined) || {};
  const participants = collectPresentParticipants(state);
  const witnesses = participants;
  const transcript = `【玩家】\n${userText}\n\n【GM】\n${assistantText}`;
  const mentioned = collectMentionedKnownEntities(transcript);
  const relatedEntities = uniq([...participants, ...mentioned]);
  const sceneId = `auto_${createHash("sha256").update(transcript).digest("hex").slice(0, 24)}`;

  const payload = {
    scene_id: sceneId,
    campaign_id: CAMPAIGN_ID,
    in_world_time: String(world.时间 || "当前"),
    location_id: world.地点 ? String(world.地点) : undefined,
    active_quest_ids: Object.keys((state.任务列表 as Record<string, unknown> | undefined) || {}),
    participants,
    witnesses,
    transcript,
    events: [
      {
        event_type: "scene_transcript",
        summary: summarizeForEvent(assistantText, userText),
        truth_status: "canonical",
        visibility: "witnessed_only",
        witness_set: witnesses,
        related_entities: relatedEntities,
        related_locations: world.地点 ? [String(world.地点)] : [],
        related_quests: Object.keys((state.任务列表 as Record<string, unknown> | undefined) || {}),
        emotional_weight: 0.1,
        importance: 0.2,
        payload: {
          auto_commit: true,
          extractor: "dest-poet deterministic visible-turn archiver v1",
          note: "Full visible in-world transcript is archived; structured durable events may be added separately by GM.",
        },
      },
    ],
  };

  return { skip: false, payload };
}

export function autoCommitTurnMemory(options: {
  userText: string;
  assistantMessage: AgentMessage | undefined;
  toolResults?: ToolResultMessage[];
}): { ok: true; skipped?: boolean; reason?: string; duplicate?: boolean } | { ok: false; reason: string } {
  if (process.env.DEST_POET_AUTO_MEMO === "0" || process.env.DEST_POET_AUTO_MEMO === "false") {
    return { ok: true, skipped: true, reason: "disabled by DEST_POET_AUTO_MEMO" };
  }
  const built = buildAutoCommitPayload(options);
  if (built.skip) return { ok: true, skipped: true, reason: built.reason };
  const result = commitPayload(built.payload);
  if (result.ok) return { ok: true, duplicate: Boolean((result as any).duplicate) };
  return { ok: false, reason: result.reason || "unknown auto memory commit failure" };
}
