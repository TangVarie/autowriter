/**
 * 写作台 · 浏览器半。
 *
 * 页签里不放表单、不放卡片：点一下就是开一个普通对话，只是这个对话的智能体预设
 * 是「写作台」（`writing-desk`）。写作台的协议和 deskcore 的工具都在这个预设里，
 * 所以点开之后就是一个正常的聊天界面，直接说话就行。
 *
 * 这里只做两件事：
 *   1. `sidebar.panellist` 里放一个「写作台」入口；
 *   2. 对应的 `main` 面板一挂载就把当前工作区切到一个写作台对话（`startSession`），
 *      导航一完成这个面板就让位给正常的 Conversation，用户看到的是聊天。
 *
 * 配置不在这里：地址和钥匙早就在本机凭据库里，要改就 `/desk-setup`（和原来的
 * 写作台插件一样）。
 */
window.__ModuleLoader__.load({
  id: 'bywood-writing-desk-dsh',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    const NS = 'writing-desk';
    const PANEL_ID = 'writing-desk';
    const PRESET_ID = 'writing-desk';
    /** 一般导航是瞬间的；这么久还停在这一页，才提示「没打开」。 */
    const FALLBACK_MS = 2500;

    const ZH = {
      panel: '写作台',
      opening: '正在打开写作台对话…',
      notOpened: '没有打开？再点一次左侧栏的「写作台」，或者先选一个工作区。',
      retry: '再开一次',
    };

    const EN = {
      panel: 'Writing desk',
      opening: 'Opening a writing-desk conversation…',
      notOpened: 'Didn’t open? Click “Writing desk” in the sidebar again, or pick a workspace first.',
      retry: 'Try again',
    };

    const CSS = [
      '.wd-launch{box-sizing:border-box;display:flex;flex:1;min-height:0;align-items:center;justify-content:center;',
      'flex-direction:column;gap:10px;padding:24px;text-align:center;',
      'color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-bg-base);}',
      '.wd-launch-text{margin:0;font-size:13px;line-height:20px;}',
      '.wd-launch-btn{font:inherit;font-size:12px;line-height:18px;padding:5px 10px;border-radius:8px;cursor:pointer;',
      'border:.5px solid var(--dsw-alias-border-l2);background:transparent;color:var(--dsw-alias-brand-primary);}',
      '.wd-launch-btn:hover{background:var(--dsw-alias-interactive-bg-hover);}',
      '.wd-launch-btn:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px;}',
    ].join('');

    /** 页签图标：一张带折角的稿纸 + 一支笔。单色，跟着 currentColor 走。 */
    function DeskIcon(props) {
      const size = props && typeof props.size === 'number' ? props.size : 16;
      return h('svg', {
        viewBox: '0 0 100 100', width: size, height: size, 'aria-hidden': true, focusable: false,
        style: { display: 'block' },
      },
        h('path', {
          d: 'M24 10h34l18 18v58a8 8 0 0 1-8 8H24a8 8 0 0 1-8-8V18a8 8 0 0 1 8-8Z',
          fill: 'currentColor', fillOpacity: 0.18,
        }),
        h('path', {
          d: 'M58 10 76 28h-12a6 6 0 0 1-6-6V10Z',
          fill: 'currentColor', fillOpacity: 0.45,
        }),
        h('rect', { x: 26, y: 40, width: 34, height: 5, rx: 2.5, fill: 'currentColor' }),
        h('rect', { x: 26, y: 52, width: 26, height: 5, rx: 2.5, fill: 'currentColor' }),
        h('path', {
          d: 'm30 78 3-10 18-18 7 7-18 18-10 3Zm21-21 7 7',
          stroke: 'currentColor', strokeWidth: 4, strokeLinecap: 'round', strokeLinejoin: 'round', fill: 'none',
        }));
    }

    /**
     * 一个只负责「开对话」的面板：挂载后立刻 `startSession`，导航完成后这个面板
     * 就被正常的 Conversation 取代。没打开时（没有工作区之类）留一句提示和重试。
     */
    function Launcher(props) {
      const { tr, open } = props;
      const started = React.useRef(false);
      const [late, setLate] = React.useState(false);

      React.useEffect(() => {
        let timer;
        if (!started.current) {
          started.current = true;
          open();
          timer = window.setTimeout(() => setLate(true), FALLBACK_MS);
        }
        return () => { if (timer) window.clearTimeout(timer); };
      }, [open]);

      return h('div', { className: 'wd-launch', role: 'status', 'aria-live': 'polite' },
        h('p', { className: 'wd-launch-text' }, tr('opening')),
        late
          ? h('p', { className: 'wd-launch-text' }, tr('notOpened'))
          : null,
        late
          ? h('button', {
            type: 'button',
            className: 'wd-launch-btn',
            onClick: () => { setLate(false); open(); },
          }, tr('retry'))
          : null,
      );
    }

    return {
      inject: ['slots', 'locale', 'uiWorkspace'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, 'zh', ZH), 'writing desk: zh dictionary');
        ctx.effect(() => ctx.locale.register(NS, 'en', EN), 'writing desk: en dictionary');
        const tr = ctx.locale.bind(NS);

        ctx.effect(() => {
          const style = document.createElement('style');
          style.setAttribute('data-dsh-plugin', NS);
          style.textContent = CSS;
          document.head.appendChild(style);
          return () => { style.remove(); };
        }, 'writing desk: styles');

        /**
         * 开一个写作台对话。
         *
         * `startSession` owns the whole navigation：它会建（或复用当前工作区的空白会话）、
         * 设成写作台预设、显示 Conversation。所以这里**不要**再选任何面板，那会把这次
         * 还没完成的导航撤掉。
         *
         * 不传 prompt：这就是一个空白的正常对话，用户自己说第一句话。
         * 写作台协议和 deskcore 的工具由「写作台」预设带进来，不用在这儿填任何东西。
         */
        const open = () => {
          try {
            ctx.uiWorkspace.startSession(undefined, { agentPreset: PRESET_ID });
          } catch {
            /* 导航失败由 Workspace 自己的通知显示；这里留了重试 */
          }
        };

        ctx.effect(
          () => ctx.slots.inject('main', () => ctx.slots.register({
            name: 'main',
            key: PANEL_ID,
            locale: NS,
            inject: () => ({ tr, open }),
          }, Launcher)),
          'writing desk: launcher panel',
        );

        ctx.effect(
          () => ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
            name: 'sidebar.panellist',
            id: PANEL_ID,
            order: 2,
            label: () => tr('panel'),
            locale: NS,
          }, DeskIcon)),
          'writing desk: sidebar entry',
        );
      },
    };
  },
});
