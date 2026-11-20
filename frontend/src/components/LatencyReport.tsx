import { useState } from "react";
import { ExternalLink, FileText, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { api, cn } from "@/lib";
import { ask } from "@/components/Confirm";

type Meta = { dir: string; title: string; runs: number[]; lint: { errors: number; warnings: number; output: string }; url: string; preview?: string };

const DEFAULT_BASIS = "《机器人遥操延迟测试计划》https://cwjgfm21di.feishu.cn/wiki/Y66vwEIKvikQpTkBxWOcd7qznub";

/** 测完一键出报告：按专项测试记录模板生成，过写作规范检查，确认后发到飞书“我的文档库”。 */
export function LatencyReport({ hasMeasures, busy }: { hasMeasures: boolean; busy: boolean }) {
  const [basis, setBasis] = useState(DEFAULT_BASIS);
  const [note, setNote] = useState("");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [phase, setPhase] = useState<"idle" | "building" | "publishing">("idle");
  const [error, setError] = useState("");

  const build = async () => {
    setPhase("building");
    setError("");
    setMeta(null);
    try {
      setMeta(await api<Meta>("/report/latency", { body: { basis, note } }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPhase("idle");
    }
  };

  const publish = async () => {
    if (!meta) return;
    setPhase("publishing");
    setError("");
    try {
      const m = await api<Meta>("/report/publish", { body: { dir: meta.dir } });
      setMeta({ ...meta, url: m.url });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPhase("idle");
    }
  };

  const lintOk = meta && meta.lint.errors === 0;

  return (
    <section className="mt-8 rounded-md border border-line bg-panel p-6">
      <h2 className="text-[16px] font-semibold">生成测试记录</h2>
      <p className="mt-1 max-w-[72ch] text-ink-soft">
        用今天的测量结果，按《专项测试记录模板》生成飞书文档：要点、测试准备、结果表（每次一段 A|B 并排视频）、对比图、逐次分布图和画面、脚本原件、结论和问题表。生成后先过写作规范检查，再由你确认发布。
      </p>
      <div className="mt-4 grid max-w-[900px] grid-cols-[1.4fr_1fr] gap-3">
        <Field label="依据文档">
          <Input value={basis} onChange={(e) => setBasis(e.target.value)} />
        </Field>
        <Field label="补充说明" hint="例如场地、Pico 版本、远程网络环境">
          <Input value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button variant="primary" disabled={!hasMeasures || busy || phase !== "idle"} onClick={build}>
          <FileText />
          {phase === "building" ? "正在生成（转视频、画图）…" : "生成测试记录"}
        </Button>
        {!hasMeasures && <span className="text-sm text-ink-soft">今天还没有测量结果</span>}
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-fail">{error}</p>}

      {meta && (
        <div className="mt-5 border-t border-line-soft pt-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <span className="font-medium">{meta.title}</span>
            <span className={cn("text-sm", lintOk ? "text-pass" : "text-fail")}>
              写作规范检查：错误 {meta.lint.errors}，警告 {meta.lint.warnings}
            </span>
            <span className="text-xs text-ink-soft">本地文件：{meta.dir}</span>
          </div>
          {!lintOk && <pre className="mt-2 max-h-40 overflow-auto rounded bg-paper p-3 text-xs">{meta.lint.output}</pre>}
          <details className="mt-3">
            <summary className="cursor-pointer text-sm text-ink-soft">看正文文字</summary>
            <pre className="mt-2 max-h-[360px] overflow-auto whitespace-pre-wrap rounded bg-paper p-3 text-[13px] leading-6">{meta.preview?.replace(/\n\s*\n+/g, "\n")}</pre>
          </details>
          <div className="mt-4 flex items-center gap-3">
            {meta.url ? (
              <>
                <span className="text-sm text-pass">已发布</span>
                <Button variant="outline" onClick={() => api("/open", { body: { url: meta.url } })}>
                  <ExternalLink />
                  打开飞书文档
                </Button>
                <span className="text-xs text-ink-soft">{meta.url}</span>
              </>
            ) : (
              <Button
                variant="live"
                disabled={!lintOk || phase !== "idle"}
                onClick={async () => {
                  if (await ask(`把“${meta.title}”发到飞书“我的文档库”？`, { ok: "发布" })) publish();
                }}
              >
                <Send />
                {phase === "publishing" ? "正在上传图片和视频…" : "发到飞书"}
              </Button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
