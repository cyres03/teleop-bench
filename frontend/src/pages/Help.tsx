import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "@/lib";

export default function Help() {
  const [text, setText] = useState("");
  useEffect(() => {
    api<string>("/docs").then(setText).catch((e) => setText(`读取使用说明失败：${e.message}`));
  }, []);
  return (
    <article className="doc max-w-[78ch]">
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </article>
  );
}
