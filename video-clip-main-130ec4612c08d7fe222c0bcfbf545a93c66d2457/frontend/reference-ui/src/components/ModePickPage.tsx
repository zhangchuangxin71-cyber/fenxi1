import type { TabMode } from "../api/types";
import { BRAND_NAME } from "../brand";

const LANDSCAPE = "/mode-mock/landscape.jpg";
const THUMBS = [
  "/mode-mock/thumb-0.jpg",
  "/mode-mock/thumb-1.jpg",
  "/mode-mock/thumb-2.jpg",
  "/mode-mock/thumb-3.jpg",
  "/mode-mock/thumb-4.jpg",
];

function IconManual() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden>
      <rect
        x="3"
        y="4"
        width="14"
        height="10"
        rx="2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      />
      <path
        d="M8 18h13M17.5 14.5L21 18l-3.5 3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconAuto() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden>
      <rect
        x="3"
        y="6"
        width="18"
        height="12"
        rx="2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      />
      <path
        d="M7 10h.01M11 10h.01M15 10h.01M7 14h10"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path
        d="M18.5 2.5l1 2 2 .4-1.5 1.4.4 2.1-1.9-1-1.9 1 .4-2.1L15.5 4.9l2-.4 1-2z"
        fill="currentColor"
      />
    </svg>
  );
}

function ModeIllustrateManual() {
  return (
    <div className="mode-mock mode-mock-ui mode-mock-manual" aria-hidden>
      <div className="mode-mock-preview">
        <img src={LANDSCAPE} alt="" className="mode-mock-photo" />
        <div className="mode-mock-play">
          <span />
        </div>
      </div>

      <div className="mode-mock-editor">
        <div className="mode-mock-rail">
          <i className="r-cursor" />
          <i className="r-film" />
          <i className="r-wave" />
        </div>
        <div className="mode-mock-tracks">
          <div className="mode-mock-filmstrip">
            {Array.from({ length: 10 }).map((_, i) => (
              <img
                key={i}
                src={THUMBS[i % THUMBS.length]}
                alt=""
                className="mode-mock-thumb-img"
              />
            ))}
            <div className="mode-mock-range">
              <span className="mode-mock-vbar left">
                <b>00:15.12</b>
              </span>
              <span className="mode-mock-vbar right">
                <b>00:45.30</b>
              </span>
            </div>
          </div>
          <div className="mode-mock-waveform">
            <svg viewBox="0 0 320 28" preserveAspectRatio="none">
              <path
                d="M0 14c4-10 8 10 12 0s8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 12 0 8 10 8 0"
                fill="none"
                stroke="#3b82f6"
                strokeWidth="2"
              />
            </svg>
          </div>
        </div>
      </div>

      <div className="mode-mock-toolbar">
        <button type="button" tabIndex={-1}>
          微调帧
        </button>
        <span className="mode-mock-tool-btns">
          <i>⏮</i>
          <i>◀</i>
          <i>▶</i>
          <i>⏭</i>
        </span>
        <button type="button" tabIndex={-1}>
          逐帧
        </button>
        <span className="mode-mock-zoom">
          <i>🔍−</i>
          <i>🔍+</i>
        </span>
      </div>
    </div>
  );
}

function ModeIllustrateAuto() {
  const scenes = ["01", "02", "03", "04", "05"];
  return (
    <div className="mode-mock mode-mock-ui mode-mock-auto" aria-hidden>
      <div className="mode-mock-preview">
        <img src={LANDSCAPE} alt="" className="mode-mock-photo" />
        <div className="mode-mock-detect-badge">
          <span className="mode-mock-spark" />
          自动分镜示意
        </div>
      </div>

      <div className="mode-mock-scenes">
        {scenes.map((n, i) => (
          <div key={n} className="mode-mock-scene-wrap">
            <div className="mode-mock-scene">
              <img src={THUMBS[i]} alt="" className="mode-mock-scene-img" />
              <em>{n}</em>
            </div>
            {i < scenes.length - 1 && (
              <span className="mode-mock-cut">✂</span>
            )}
          </div>
        ))}
      </div>

      <div className="mode-mock-wave-bar" />
    </div>
  );
}

function ModeCards({ onSelectMode }: { onSelectMode: (mode: TabMode) => void }) {
  return (
    <div className="mode-cards mode-cards-ref">
      <article className="mode-ref-card mode-ref-manual">
        <div className="mode-ref-head">
          <span className="mode-ref-icon blue" aria-hidden>
            <IconManual />
          </span>
          <div className="mode-ref-titles">
            <div className="mode-ref-title-row">
              <h3>手动切片</h3>
              <span className="mode-pick-badge blue">精准控制</span>
            </div>
            <p>手动标记入点和出点，精确到帧，完全掌控每一段片段</p>
          </div>
        </div>
        <ModeIllustrateManual />
        <ul className="mode-ref-features mode-ref-checks">
          <li>
            <span className="mode-ref-check" aria-hidden>
              ✓
            </span>
            帧级精度
          </li>
          <li>
            <span className="mode-ref-check" aria-hidden>
              ✓
            </span>
            自由添加片段
          </li>
          <li>
            <span className="mode-ref-check" aria-hidden>
              ✓
            </span>
            实时预览
          </li>
          <li>
            <span className="mode-ref-check" aria-hidden>
              ✓
            </span>
            逐帧微调
          </li>
        </ul>
        <button
          type="button"
          className="mode-pick-btn blue"
          onClick={() => onSelectMode("manual")}
        >
          进入手动切片模式 <span aria-hidden>→</span>
        </button>
      </article>

      <article className="mode-ref-card mode-ref-auto">
        <div className="mode-ref-head">
          <span className="mode-ref-icon purple" aria-hidden>
            <IconAuto />
          </span>
          <div className="mode-ref-titles">
            <div className="mode-ref-title-row">
              <h3>自动分镜</h3>
              <span className="mode-pick-badge purple">智能高效</span>
            </div>
            <p>智能检测场景变化，自动识别分镜，一键生成片段</p>
          </div>
        </div>
        <ModeIllustrateAuto />
        <div className="mode-ref-algos">
          <div className="mode-ref-algos-title">支持多种检测算法</div>
          <div className="mode-ref-algo-list">
            <div className="mode-ref-algo">
              <span className="mode-ref-algo-ico" aria-hidden>
                ▦
              </span>
              <span>画面切换</span>
            </div>
            <div className="mode-ref-algo">
              <span className="mode-ref-algo-ico" aria-hidden>
                ⚡
              </span>
              <span>抗闪切镜</span>
            </div>
            <div className="mode-ref-algo">
              <span className="mode-ref-algo-ico" aria-hidden>
                ✦
              </span>
              <span>语义分镜</span>
            </div>
          </div>
        </div>
        <button
          type="button"
          className="mode-pick-btn purple"
          onClick={() => onSelectMode("auto")}
        >
          进入自动分镜模式 <span aria-hidden>→</span>
        </button>
      </article>
    </div>
  );
}

function ModeWhySection() {
  return (
    <section className="mode-why">
      <h3 className="mode-why-title">
        为什么选择 <span className="mode-brand-grad">{BRAND_NAME}</span>？
      </h3>
      <div className="mode-why-grid">
        <div className="mode-why-item">
          <span className="mode-why-ico blue" aria-hidden>
            ⚡
          </span>
          <strong>速度快</strong>
          <p>基于 FFmpeg + 并行处理，高效切割不等待</p>
        </div>
        <div className="mode-why-item">
          <span className="mode-why-ico blue" aria-hidden>
            ◎
          </span>
          <strong>精度高</strong>
          <p>帧级精度切割，确保片段无误差</p>
        </div>
        <div className="mode-why-item">
          <span className="mode-why-ico purple" aria-hidden>
            ◧
          </span>
          <strong>易操作</strong>
          <p>可视化时间轴，简单直观好上手</p>
        </div>
        <div className="mode-why-item">
          <span className="mode-why-ico purple" aria-hidden>
            ↓
          </span>
          <strong>易导出</strong>
          <p>支持单段导出或一键获取 OSS 地址</p>
        </div>
      </div>
      <p className="mode-why-tip mode-why-tip-amber">
        💡 小贴士：进入工作台后如需换模式，请返回本步重新选择
      </p>
    </section>
  );
}

export function ModePickPage({
  onSelectMode,
}: {
  onSelectMode: (mode: TabMode) => void;
}) {
  return (
    <div className="home-landing home-landing-pick-mode home-landing-mode-ref">
      <div className="mode-page-ambient" aria-hidden>
        <span className="mode-orb mode-orb-a" />
        <span className="mode-orb mode-orb-b" />
        <span className="mode-orb mode-orb-c" />
      </div>
      <div className="home-landing-main">
        <header className="home-mode-hero home-mode-hero-ref">
          <span className="mode-hero-deco mode-hero-deco-left" aria-hidden />
          <span className="mode-hero-deco mode-hero-deco-right" aria-hidden />
          <h2 className="home-mode-section-title">
            <span className="mode-brand-grad">{BRAND_NAME}</span>
            {" · "}
            选择切片模式
          </h2>
          <p className="home-mode-lead">
            手动精剪，或交给算法自动分镜——选定后进入预览与编辑
          </p>
          <p className="mode-hero-tip">
            <span className="mode-hero-tip-ico" aria-hidden>
              ℹ
            </span>
            提示：进入工作台后模式锁定；换模式请返回本步。换片请点顶栏回到「选择源片」
          </p>
        </header>
        <ModeCards onSelectMode={onSelectMode} />
        <ModeWhySection />
      </div>
    </div>
  );
}
