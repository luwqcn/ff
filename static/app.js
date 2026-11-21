/* ff · 博客目录：独立博客为主（每源最多 5 篇），博客聚集每源 2 篇，仅显示近两天 */
(() => {
  'use strict';
  const boot = window.FF_BOOT;
  if (!boot) return;
  const $ = (id) => document.getElementById(id);
  const FRESH_MS = 48 * 60 * 60 * 1000;
  const LIMITS = { '独立博客': 5, '博客聚集': 2 };
  const DEFAULT_LIMIT = 2;

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function plain(s) {
    return String(s || '')
      .replace(/<[^>]*>/g, ' ')
      .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&#x27;/g, "'").replace(/\s+/g, ' ').trim();
  }

  function thumb(s) {
    const m = String(s || '').match(/<img[^>]+src=["']([^"']+)["']/i);
    return m ? m[1] : '';
  }

  function relTime(iso) {
    const t = Date.parse(iso || '');
    if (!Number.isFinite(t)) return '';
    const diff = Date.now() - t;
    if (diff < 60 * 1000) return '刚刚';
    const min = Math.floor(diff / 60000);
    if (min < 60) return `${min} 分钟前`;
    const hour = Math.floor(min / 60);
    if (hour < 24) return `${hour} 小时前`;
    const d = new Date(t);
    const clock = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    const day = Math.floor(hour / 24);
    return day === 1 ? `昨天 ${clock}` : `${day} 天前`;
  }

  let read = new Set();
  try { read = new Set(JSON.parse(localStorage.getItem('ff.read') || '[]')); } catch (_) {}
  let activeTag = null;   // 单选标签；只在当前分类内过滤

  function render() {
    const byFeed = new Map();
    const now = Date.now();
    for (const a of boot.articles || []) {
      // 生成时已过滤一次；这里按访问时间再过滤，缓存的旧页面也不会显示过期文章
      const t = Date.parse(a.published || '');
      if (!Number.isFinite(t) || now - t > FRESH_MS) continue;
      if (!byFeed.has(a.feed_slug)) byFeed.set(a.feed_slug, []);
      byFeed.get(a.feed_slug).push(a);
    }
    // 标签从全部启用源聚合，不受"近两天有更新"影响
    const allFeeds = (boot.feeds || []).map((f) => {
      const posts = byFeed.get(f.slug) || [];
      const limit = LIMITS[f.category] ?? DEFAULT_LIMIT;
      return { ...f, posts: posts.slice(0, limit), latest: posts[0]?.published || '' };
    });
    const active = allFeeds.filter((f) => f.posts.length);
    active.sort((a, b) => b.latest.localeCompare(a.latest));

    if (!active.length) {
      $('catalog').innerHTML = '<p class="empty">近两天暂无新文章，过些时候再来看看。</p>';
      return 0;
    }

    // 分类顺序以"有更新的源"为准；标签栏则按全部启用源统计
    const catOf = new Map();
    for (const f of active) if (!catOf.has(f.category)) catOf.set(f.category, []);
    const names = [...catOf.keys()].sort((a, b) => {
      const preferred = ['独立博客', '博客聚集'];
      const ai = preferred.indexOf(a), bi = preferred.indexOf(b);
      if (ai >= 0 && bi >= 0) return ai - bi;
      if (ai >= 0) return -1;
      if (bi >= 0) return 1;
      return a.localeCompare(b, 'zh');
    });

    const notes = new Map((boot.categories || []).map((c) => [c.name, c.description || '']));
    const tagCount = new Map();   // "分类|标签" -> 启用源个数
    for (const f of allFeeds) {
      for (const t of f.tags || []) {
        const key = f.category + '|' + t;
        tagCount.set(key, (tagCount.get(key) || 0) + 1);
      }
    }

    $('catalog').innerHTML = names.map((name) => {
      const updated = active.filter((f) => f.category === name);
      // 标签条：本分类全部启用源里出现过的标签
      const tags = [...new Set(allFeeds.filter((f) => f.category === name)
        .flatMap((f) => f.tags || []))].sort((a, b) => a.localeCompare(b, 'zh'));
      const allOn = !(activeTag && activeTag.category === name);
      const tagBar = tags.length ? `
        <div class="tags">
          <button type="button" class="tag-chip tag-all${allOn ? ' on' : ''}" data-category="${esc(name)}" data-tag="" aria-pressed="${allOn ? 'true' : 'false'}">全部</button>
          ${tags.map((t) => {
            const on = activeTag && activeTag.category === name && activeTag.tag === t;
            return `<button type="button" class="tag-chip${on ? ' on' : ''}" data-category="${esc(name)}" data-tag="${esc(t)}" aria-pressed="${on ? 'true' : 'false'}">${esc(t)} <span class="tag-num">${tagCount.get(name + '|' + t) || 0}</span></button>`;
          }).join('')}
        </div>` : '';

      const filtered = activeTag && activeTag.category === name
        ? updated.filter((f) => (f.tags || []).includes(activeTag.tag))
        : updated;
      const body = filtered.length
        ? filtered.map((f) => `
          <div class="source">
            <div class="src-head">
              <span class="src-dot"></span>
              <a class="src-link src-name" href="${esc(f.link || f.url)}" target="_blank" rel="noopener noreferrer">${esc(f.title)}</a>
              <span class="src-count">${f.posts.length} 篇</span>
            </div>
            ${f.description ? `<p class="src-desc">${f.description}</p>` : ''}
            <div class="cards">
              ${f.posts.map((a) => {
                const img = thumb(a.summary);
                return `
              <article class="card ${read.has(a.guid) ? 'read' : ''}" data-guid="${esc(a.guid)}" data-link="${esc(a.link || '#')}">
                <div class="card-top">
                  <span class="card-src">${esc(f.title)}</span>
                  <time>${relTime(a.published)}</time>
                </div>
                <div class="card-body">
                  <div class="card-text">
                    <h3 class="card-title"><a href="${esc(a.link || '#')}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a></h3>
                    <p class="card-summary">${esc(plain(a.summary)) || '（该源未提供摘要）'}</p>
                  </div>
                  ${img ? `<a class="card-thumb" href="${esc(a.link || '#')}" target="_blank" rel="noopener noreferrer"><img src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer"></a>` : ''}
                </div>
              </article>`;
              }).join('')}
            </div>
          </div>`).join('')
        : `<p class="tag-empty">「${esc(activeTag.tag)}」近两天暂无更新</p>`;

      return `
      <section class="group">
        <div class="group-head">
          <h2 class="group-title">${esc(name)}</h2>
          <span class="group-rule"></span>
          <span class="group-count">${filtered.length} 个源有更新</span>
        </div>
        ${notes.get(name) ? `<p class="group-note">${notes.get(name)}</p>` : ''}
        ${tagBar}
        ${body}
      </section>`;
    }).join('');
    return active.length;
  }

  // 标签单选：点已选中的取消，点别的切换；过滤只在当前分类内发生
  $('catalog').addEventListener('click', (e) => {
    const chip = e.target.closest('.tag-chip');
    if (!chip) return;
    const category = chip.dataset.category, tag = chip.dataset.tag;
    if (!tag) activeTag = null;   // 「全部」：清除筛选，恢复该分类全部有更新的源
    else if (activeTag && activeTag.category === category && activeTag.tag === tag) activeTag = null;
    else activeTag = { category, tag };
    render();
  });

  // 整张卡片可点：点标题/缩略图走原生链接（支持右键新标签页），点卡片其余区域也跳文章
  $('catalog').addEventListener('click', (e) => {
    const card = e.target.closest('.card');
    if (!card) return;
    const nativeLink = e.target.closest('a');
    if (nativeLink && nativeLink.closest('.card') === card) {
      // 原生链接自己跳转，这里只记已读
    } else {
      const url = card.dataset.link;
      if (url) window.open(url, '_blank', 'noopener');
    }
    const guid = card.dataset.guid;
    if (!guid || read.has(guid)) return;
    read.add(guid);
    try { localStorage.setItem('ff.read', JSON.stringify([...read])); } catch (_) {}
    card.classList.add('read');
  });

  const activeFeeds = render();
  $('footStat').textContent =
    `${activeFeeds} 个源近两天有更新 · 独立博客每源最多 5 篇、博客聚集 2 篇 · 更新于 ${relTime(boot.meta.fetched_at || boot.meta.built_at)}`;
})();
