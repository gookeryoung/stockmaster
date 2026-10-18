/** 工作区页内锚点子导航。
 *
 * 不新增路由：分区靠 `#overview` 这类 hash 定位，点击后由浏览器滚动到对应
 * `<section id>`；IntersectionObserver 只负责“当前块高亮”，不改变页面状态。
 */

import { useEffect, useState } from "react";

const SECTIONS = [
  { id: "overview", label: "概览" },
  { id: "members", label: "成员" },
  { id: "notifications", label: "通知" },
  { id: "usage", label: "用量与策略" },
] as const;

export default function WorkspaceSectionNav() {
  const [active, setActive] = useState<string>("overview");

  useEffect(() => {
    // 支持从外部链接直接落到某个分区（例如“邀请成员 → /workspace#members”）。
    const hash = window.location.hash.replace("#", "");
    if (SECTIONS.some((item) => item.id === hash)) {
      const target = document.getElementById(hash);
      if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
    }

    const elements = SECTIONS.map((item) => document.getElementById(item.id)).filter(
      (element): element is HTMLElement => element !== null,
    );
    if (elements.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActive(visible[0].target.id);
      },
      // 顶部让出 sticky 头/子导航的高度，底部让出大半屏，避免相邻区块来回跳。
      { rootMargin: "-96px 0px -55% 0px", threshold: [0, 0.1, 0.5] },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, []);

  return (
    <nav
      aria-label="工作区页内导航"
      className="sticky top-0 z-10 -mx-3 overflow-x-auto border-b border-slate-800 bg-slate-950/95 px-3 py-1.5 backdrop-blur sm:-mx-4 sm:px-4"
    >
      <div className="flex gap-1">
        {SECTIONS.map((item) => (
          <a
            key={item.id}
            href={`#${item.id}`}
            aria-current={active === item.id ? "true" : undefined}
            onClick={() => setActive(item.id)}
            className={`shrink-0 rounded-md px-3 py-1.5 text-sm transition ${
              active === item.id
                ? "bg-sky-600 text-white"
                : "text-slate-300 hover:bg-slate-800 hover:text-white"
            }`}
          >
            {item.label}
          </a>
        ))}
      </div>
    </nav>
  );
}
