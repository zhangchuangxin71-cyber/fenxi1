import { BRAND_NAME } from "../brand";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function OperationGuide({ open, onClose }: Props) {
  if (!open) return null;

  return (
    <div
      className="guide-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="guide-title"
      onClick={onClose}
    >
      <div
        className="guide-dialog guide-dialog-wide"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="guide-dialog-head">
          <h2 id="guide-title">操作指南</h2>
          <button
            type="button"
            className="guide-close"
            onClick={onClose}
            aria-label="关闭"
          >
            ×
          </button>
        </div>

        <div className="guide-body">
          <p className="guide-intro">
            本系统 <strong>{BRAND_NAME}</strong>{" "}
            用于视频切片：粘贴 OSS Key 或 URL 导入源片，选择手动 / 自动模式编辑，后台切割后返回 OSS 切片地址。整体流程为
            <strong>
              {" "}
              导入源片 → 选择切片模式 → 预览与编辑 → 切割与保存
            </strong>
            。顶栏步骤条可回退到已完成步骤（例如从编辑页回到选模式）。
          </p>

          <section className="guide-section">
            <h3>一、导入源片</h3>
            <ol className="guide-steps">
              <li>
                首页粘贴 <code>oss_key</code>（对象键）或完整 HTTPS URL，点「导入源片」。导入为异步，完成后显示预览与基础信息。
              </li>
              <li>
                导入成功后进入确认页：左侧<strong>视频预览</strong>、右侧<strong>视频信息</strong>
                ；确认无误后点「下一步：选择切片模式」。
              </li>
              <li>
                若要换片：在本步确认页点「更换源片」，或之后任意步点击顶栏「选择源片」再换。
              </li>
            </ol>
          </section>

          <section className="guide-section">
            <h3>二、选择切片模式</h3>
            <ol className="guide-steps">
              <li>
                下方选择切片模式（手动切片 / 自动分镜）。
              </li>
              <li>
                <strong>手动切片：</strong>
                在时间轴标记入点 / 出点，适合需要帧级精度的剪辑。
              </li>
              <li>
                <strong>自动分镜：</strong>
                由算法自动找切点，再人工预览、勾选与微调，适合长视频快速粗切。
              </li>
              <li>
                进入「预览与编辑」后模式锁定；若要更换，请点击顶栏步骤回到「选择切片模式」重新进入。
              </li>
            </ol>
          </section>

          <section className="guide-section">
            <h3>三、手动切片（预览与编辑）</h3>
            <p className="guide-note">
              左侧为「视频预览 + 时间轴」，中间为「当前选区」控制台，右侧为片段列表。预览与封面按源片原始比例显示（竖屏竖框、横屏横框）。
            </p>
            <ol className="guide-steps">
              <li>
                <strong>视频预览缩放：</strong>
                拖动预览框四角或边缘手柄放大 / 缩小（约 50%～200%）；左上角「Fit」复位。放大后可在视频区内拖拽平移。时间轴工具栏「Fit」表示显示全片时间范围。
              </li>
              <li>
                <strong>标记选区：</strong>
                按 <kbd>I</kbd> / <kbd>O</kbd>{" "}
                标记入点、出点，或在时间轴上拖选。控制台显示入点、出点、时长与帧数。
              </li>
              <li>
                <strong>微调：</strong>
                帧步进移动播放头；
                <strong>−5s / −1s / +1s / +5s</strong> 按秒跳转；
                <strong>入点 / 出点 ±1 帧</strong> 调整边界。
              </li>
              <li>
                <strong>操作区：</strong>
                「播放选区」预览当前区间，可勾选循环。按 <kbd>Enter</kbd>{" "}
                或「+ 添加片段」写入右侧列表；「清理临时选区」只清入出点，不删列表。
              </li>
              <li>
                <strong>片段列表：</strong>
                「选中微调」载入该片段；选中后可「更新片段」。确认无误后点「下一步：切割与保存」。
              </li>
            </ol>
            <div className="guide-kbd-block">
              <span>
                <kbd>Space</kbd> 播放选区 / 暂停
              </span>
              <span>
                <kbd>←</kbd> / <kbd>→</kbd> 帧步进
              </span>
              <span>
                <kbd>J</kbd> / <kbd>K</kbd> ±1s
              </span>
              <span>
                <kbd>I</kbd> / <kbd>O</kbd> 标记入点/出点
              </span>
              <span>
                <kbd>Enter</kbd> 添加片段
              </span>
              <span>
                <kbd>L</kbd> 循环播放
              </span>
              <span>
                <kbd>Del</kbd> 有选中则删除该项，否则清理临时选区
              </span>
              <span>
                <kbd>Ctrl</kbd> + <kbd>Z</kbd> / <kbd>Y</kbd> 撤销/重做
              </span>
              <span>
                <kbd>Ctrl</kbd> + 滚轮 时间轴缩放
              </span>
            </div>
          </section>

          <section className="guide-section">
            <h3>四、自动分镜（预览与编辑）</h3>
            <ol className="guide-steps">
              <li>
                选择算法：
                <strong>画面切镜</strong>（本地、按画面变化）、
                <strong>语义分镜 AI大模型</strong>（联网调用，按话题切分，耗时更长）、
                <strong>抗闪切镜</strong>（抑制闪烁误切）。
              </li>
              <li>
                设置<strong>切分敏感度</strong>（精准 / 标准 / 粗略，带推荐说明）或自定义「切分敏感度 / 最短片段」等参数。
              </li>
              <li>
                点「开始自动切分」等待检测结果；若勾选「切分后直接导出」，则跳过预览确认，检测后自动进入切割（按钮文案为「切分并导出」）。
              </li>
              <li>
                场景列表最前为 ▶ 播放，最后为勾选框。可在列表与时间轴上预览、合并、删除或拖动边界；勾选要切的分镜（未勾选则默认全部）。
              </li>
              <li>
                点「确认切分」进入切割；仅切割勾选项（未勾选则全部）。若需调整，点「返回场景列表」或「返回场景列表重新拆分」，修改后再「重新切分 / 确认切分」。
              </li>
            </ol>
          </section>

          <section className="guide-section">
            <h3>五、切割与保存</h3>
            <ol className="guide-steps">
              <li>
                执行页用霓虹进度条展示真实切割进度；文案来自任务状态与就绪片段数。可点「刷新状态」查看最新进度。
              </li>
              <li>
                切割在后台进行，可返回编辑页；顶栏可再次进入「切割与保存」查看进度与结果。
              </li>
              <li>
                切割成功后可预览各片段封面与播放；封面比例与源片一致。在同一页获取全部或选中片段的 OSS 地址。
              </li>
              <li>
                「获取 OSS 地址」只返回对象列表，不会复制或写入其他媒资库。勾选要返回的片段；未勾选则返回全部。
              </li>
            </ol>
          </section>

          <section className="guide-section">
            <h3>六、界面说明</h3>
            <ul className="guide-steps guide-steps-bullets">
              <li>
                顶栏四步：选择源片 → 选择切片模式 → 预览与编辑 → 切割与保存；已完成步骤可点击回退。第一步导入后左侧预览、右侧视频信息，第二步专注选模式。
              </li>
              <li>
                工作台内模式锁定；换模式请回到第二步「选择切片模式」。
              </li>
              <li>
                手动模式：左栏预览与时间轴、中栏选区控制台、右侧片段列表。
              </li>
              <li>
                预览框四角 / 边缘手柄可缩放视频；「Fit」复位预览大小。时间轴「Fit」复位为全片时间范围。
              </li>
              <li>
                时间轴「视频」轨为胶片缩略图，「音频」轨为波形（无声源则提示无法显示）。
              </li>
              <li>
                大文件切割在后台排队执行，可同时处理多个任务。
              </li>
            </ul>
          </section>
        </div>

        <button type="button" className="btn-detect guide-ok" onClick={onClose}>
          知道了
        </button>
      </div>
    </div>
  );
}
