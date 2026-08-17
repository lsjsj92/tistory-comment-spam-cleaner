// app/web/static/js/comments.js
//
// 댓글 관리 화면. 이 서비스의 핵심 화면이다.
//
// 호출 API
//   GET  /api/targets                 게시글 칩 구성
//   GET  /api/comments?...            목록 조회
//   POST /api/comments/select-ids     필터 결과 전체 선택
//   POST /api/comments/rescore        스팸 재분류
//   POST /api/backups/export          백업 내보내기 (삭제 작업을 만들지 않고 파일만 생성)
//   POST /api/jobs/delete             삭제 실행 (실제 삭제일 때만 백업을 만든다)
//   GET  /api/jobs/{id}/stream        진행률 (core.js 진행률 패널)
//
// 안전 원칙
//   1. 닉네임과 본문은 공격자가 넣은 값이다. 항상 textContent 로만 넣는다.
//   2. 화이트리스트와 운영자 댓글은 체크박스를 비활성화해 선택 자체를 막는다.
//   3. 삭제는 항상 사용자가 확인한 ID 목록 그대로 보낸다. 필터를 다시 평가하지 않으므로
//      확인 모달에 표시한 건수와 실제 삭제 대상이 어긋나지 않는다.
//   4. 필터 조건이 바뀌면 선택을 비운다. 조건이 달라진 선택은 사용자가 확인한 선택이 아니다.
//   5. 확인 모달은 건수만이 아니라 등급별 내역을 보여준다. 5,383 과 5,392 를 건수만으로는
//      구별할 수 없어 정상 댓글이 딸려 들어간 것을 알아챌 수 없기 때문이다.
//   6. 백업 여부는 서버 설정(APP_BACKUP_BEFORE_DELETE)을 읽어 사실대로만 표시한다.
//   7. 정상 등급이 섞이면 서버가 400 으로 거부한다. 이때는 토스트가 아니라 별도 확인 단계를
//      띄우고, 사용자가 체크박스를 켠 경우에만 allow_normal 을 붙여 재요청한다.

(function () {
  'use strict';

  var el = TG.el;
  var clear = TG.clear;
  var COLSPAN = 7;

  var refs = {
    form: document.getElementById('app-filter-form'),
    entryChips: document.getElementById('app-entry-chips'),
    levelChips: document.getElementById('app-level-chips'),
    statusChips: document.getElementById('app-status-chips'),
    dateFrom: document.getElementById('app-filter-date-from'),
    dateTo: document.getElementById('app-filter-date-to'),
    nickname: document.getElementById('app-filter-nickname'),
    content: document.getElementById('app-filter-content'),
    minScore: document.getElementById('app-filter-min-score'),
    applyBtn: document.getElementById('app-filter-apply'),
    resetBtn: document.getElementById('app-filter-reset'),
    rescoreBtn: document.getElementById('app-rescore-btn'),

    body: document.getElementById('app-comments-body'),
    resultCount: document.getElementById('app-result-count'),
    resultSummary: document.getElementById('app-result-summary'),
    selectPage: document.getElementById('app-select-page'),
    selectAllFiltered: document.getElementById('app-select-all-filtered'),

    pagerInfo: document.getElementById('app-pager-info'),
    pagePrev: document.getElementById('app-page-prev'),
    pageNext: document.getElementById('app-page-next'),
    pageForm: document.getElementById('app-page-form'),
    pageInput: document.getElementById('app-page-input'),
    pageTotal: document.getElementById('app-page-total'),

    actionbar: document.getElementById('app-actionbar'),
    selectedCount: document.getElementById('app-selected-count'),
    clearSelection: document.getElementById('app-clear-selection'),
    protectedHint: document.getElementById('app-protected-hint'),
    dryRun: document.getElementById('app-dry-run'),
    backupBtn: document.getElementById('app-backup-btn'),
    deleteBtn: document.getElementById('app-delete-btn'),

    progress: document.getElementById('app-comments-progress'),
    result: document.getElementById('app-comments-result')
  };

  var progressPanel = TG.jobProgressPanel(refs.progress);

  var state = {
    entryIds: [],       // 선택된 게시글 번호 (빈 배열이면 전체)
    levels: [],         // 빈 배열이면 전체 등급
    statuses: ['active'],
    page: 1,
    size: TG.pageSize(),
    total: 0,
    items: [],
    targets: [],        // entry_id -> 제목 조회용
    selected: new Set(),
    loading: false,
    // 마지막으로 조회한 필터 조건의 지문. 조건이 바뀌면 선택을 비우는 판단에 쓴다.
    filterSignature: null,
    // 화면에 실제로 표시된 목록의 필터 조건. 전체 선택은 반드시 이 값을 쓴다.
    // 칩은 누르는 즉시 state 를 바꾸지만 목록은 "필터 적용" 을 눌러야 다시 조회된다.
    // 그 사이에 filterParams() 를 쓰면 화면과 다른 집합이 선택된다.
    appliedParams: {},
    // .env 의 APP_BACKUP_BEFORE_DELETE. null 이면 아직 확인하지 못한 상태다.
    backupBeforeDelete: null
  };

  var targetTitles = new Map();
  // 화면에 한 번이라도 그려진 댓글의 등급. 선택 내역을 정확히 세는 데 쓴다.
  var levelById = new Map();

  // ==========================================================================
  // 필터 직렬화
  // ==========================================================================

  /**
   * 서버로 보낼 필터 조건을 만든다.
   * GET 질의 문자열과 POST /api/comments/select-ids 본문이 같은 키와 같은 값 형식을 쓰도록
   * 한곳에서만 생성한다. 배열은 계약대로 쉼표로 이어 붙인다.
   */
  function filterParams() {
    var params = {};
    if (state.entryIds.length) params.entry_ids = state.entryIds.join(',');
    if (refs.dateFrom.value.trim()) params.date_from = refs.dateFrom.value.trim();
    if (refs.dateTo.value.trim()) params.date_to = refs.dateTo.value.trim();
    if (refs.nickname.value.trim()) params.nickname = refs.nickname.value.trim();
    if (refs.content.value.trim()) params.content = refs.content.value.trim();
    if (state.levels.length) params.levels = state.levels.join(',');
    if (state.statuses.length) params.statuses = state.statuses.join(',');
    var minScore = refs.minScore.value.trim();
    if (minScore !== '') params.min_score = minScore;
    return params;
  }

  /**
   * 필터 조건의 지문. 페이지 번호는 제외하므로 페이지를 넘기는 것만으로는 바뀌지 않는다.
   * 조건이 달라졌는데 이전 선택이 남아 있으면 사용자가 무엇을 지우는지 알 수 없으므로
   * 이 값이 바뀌면 선택을 비운다.
   */
  function filterSignature() {
    return TG.buildQuery(filterParams());
  }

  /** 주소창을 현재 필터로 맞춘다. 새로 고침해도 같은 화면이 나온다. */
  function syncUrl() {
    var params = filterParams();
    params.page = state.page;
    var query = TG.buildQuery(params);
    var next = window.location.pathname + (query ? '?' + query : '');
    window.history.replaceState(null, '', next);
  }

  /** 주소창의 질의 문자열로 필터 초기값을 채운다. */
  function readUrl() {
    var search = new URLSearchParams(window.location.search);

    var entryIds = (search.get('entry_ids') || '')
      .split(',')
      .map(function (value) {
        return parseInt(value, 10);
      })
      .filter(function (value) {
        return isFinite(value);
      });
    state.entryIds = entryIds;

    var levels = (search.get('levels') || '').split(',').filter(Boolean);
    state.levels = levels.filter(function (level) {
      return ['spam', 'suspicious', 'normal'].indexOf(level) >= 0;
    });

    var statuses = (search.get('statuses') || '').split(',').filter(Boolean);
    state.statuses = statuses.filter(function (status) {
      return ['active', 'deleted'].indexOf(status) >= 0;
    });
    if (!state.statuses.length) state.statuses = ['active'];

    refs.dateFrom.value = search.get('date_from') || '';
    refs.dateTo.value = search.get('date_to') || '';
    refs.nickname.value = search.get('nickname') || '';
    refs.content.value = search.get('content') || '';
    refs.minScore.value = search.get('min_score') || '';

    var page = parseInt(search.get('page') || '1', 10);
    state.page = isFinite(page) && page > 0 ? page : 1;
  }

  // ==========================================================================
  // 칩
  // ==========================================================================

  function paintChipGroup(container, isActive) {
    TG.qsa('.app-chip', container).forEach(function (chip) {
      chip.setAttribute('aria-pressed', isActive(chip) ? 'true' : 'false');
    });
  }

  function paintLevelChips() {
    paintChipGroup(refs.levelChips, function (chip) {
      var level = chip.getAttribute('data-level');
      return level ? state.levels.indexOf(level) >= 0 : state.levels.length === 0;
    });
  }

  function paintStatusChips() {
    paintChipGroup(refs.statusChips, function (chip) {
      return state.statuses.indexOf(chip.getAttribute('data-status')) >= 0;
    });
  }

  function paintEntryChips() {
    paintChipGroup(refs.entryChips, function (chip) {
      var raw = chip.getAttribute('data-entry');
      if (!raw) return state.entryIds.length === 0;
      return state.entryIds.indexOf(parseInt(raw, 10)) >= 0;
    });
  }

  function buildEntryChips(targets) {
    clear(refs.entryChips);

    refs.entryChips.appendChild(
      el('button', {
        className: 'app-chip',
        text: '전체',
        attrs: { type: 'button', 'data-entry': '', 'aria-pressed': 'false' },
        on: {
          click: function () {
            state.entryIds = [];
            paintEntryChips();
          }
        }
      })
    );

    targets.forEach(function (target) {
      var label = target.title || ('게시글 ' + target.entry_id);
      refs.entryChips.appendChild(
        el(
          'button',
          {
            className: 'app-chip',
            title: label + ' (글번호 ' + target.entry_id + ')',
            attrs: {
              type: 'button',
              'data-entry': String(target.entry_id),
              'aria-pressed': 'false'
            },
            on: {
              click: function () {
                var index = state.entryIds.indexOf(target.entry_id);
                if (index >= 0) state.entryIds.splice(index, 1);
                else state.entryIds.push(target.entry_id);
                paintEntryChips();
              }
            }
          },
          [
            el('span', { text: TG.truncate(label, 18) }),
            el('span', {
              className: 'app-chip__count',
              text: TG.formatNumber(target.comment_count)
            })
          ]
        )
      );
    });

    paintEntryChips();
  }

  refs.levelChips.addEventListener('click', function (event) {
    var chip = event.target.closest('.app-chip');
    if (!chip) return;
    var level = chip.getAttribute('data-level');
    if (!level) {
      state.levels = [];
    } else {
      var index = state.levels.indexOf(level);
      if (index >= 0) state.levels.splice(index, 1);
      else state.levels.push(level);
    }
    paintLevelChips();
  });

  refs.statusChips.addEventListener('click', function (event) {
    var chip = event.target.closest('.app-chip');
    if (!chip) return;
    var status = chip.getAttribute('data-status');
    var index = state.statuses.indexOf(status);
    if (index >= 0) state.statuses.splice(index, 1);
    else state.statuses.push(status);
    // 상태를 모두 끄면 서버 기본값(active)과 같아지므로 화면과 결과가 어긋난다. 하나는 남긴다.
    if (!state.statuses.length) state.statuses = [status];
    paintStatusChips();
  });

  // ==========================================================================
  // 선택 상태
  // ==========================================================================

  function isProtected(comment) {
    return !!(comment.whitelisted || comment.is_admin);
  }

  function updateSelectionUi() {
    var count = state.selected.size;
    refs.selectedCount.textContent = '선택 ' + TG.formatNumber(count) + '건';
    refs.actionbar.classList.toggle('app-actionbar--visible', count > 0);
    refs.deleteBtn.disabled = count === 0;
    refs.backupBtn.disabled = count === 0;

    var selectable = state.items.filter(function (item) {
      return !isProtected(item);
    });
    var selectedOnPage = selectable.filter(function (item) {
      return state.selected.has(item.comment_id);
    }).length;

    refs.selectPage.disabled = selectable.length === 0;
    refs.selectPage.checked = selectable.length > 0 && selectedOnPage === selectable.length;
    refs.selectPage.indeterminate = selectedOnPage > 0 && selectedOnPage < selectable.length;

    TG.qsa('tr[data-comment-id]', refs.body).forEach(function (row) {
      var id = Number(row.getAttribute('data-comment-id'));
      var checkbox = row.querySelector('input[type="checkbox"]');
      var chosen = state.selected.has(id);
      if (checkbox && !checkbox.disabled) checkbox.checked = chosen;
      row.setAttribute('aria-selected', chosen ? 'true' : 'false');
    });
  }

  // ==========================================================================
  // 목록 렌더링
  // ==========================================================================

  function spamBadge(comment) {
    var level = TG.SPAM_LEVEL[comment.spam_level] || TG.SPAM_LEVEL.normal;
    var reasons = comment.spam_reasons || [];
    var tooltip =
      '점수 ' + TG.formatNumber(comment.spam_score || 0) +
      (reasons.length ? ', 적중 규칙: ' + reasons.join(', ') : ', 적중한 규칙 없음');

    return el('div', { className: 'app-row', attrs: { style: 'gap:6px;flex-wrap:nowrap' } }, [
      TG.badge(level.label, level.tone, tooltip),
      el('span', {
        className: 'app-muted-soft app-text-sm',
        text: TG.formatNumber(comment.spam_score || 0),
        title: tooltip
      })
    ]);
  }

  function statusCell(comment) {
    var info = TG.COMMENT_STATUS[comment.status] || TG.COMMENT_STATUS.active;
    var nodes = [TG.badge(info.label, info.tone, comment.deleted_at ? '삭제 시각 ' + TG.formatDateTime(comment.deleted_at) : null)];
    if (comment.is_secret) nodes.push(TG.badge('비밀', 'gray'));
    if (comment.is_reply) nodes.push(TG.badge('답글', 'gray'));
    return el('div', { className: 'app-row', attrs: { style: 'gap:5px' } }, nodes);
  }

  function nicknameCell(comment) {
    var nodes = [
      el('div', {
        className: 'app-cell-title app-truncate app-cell-name',
        text: comment.nickname || '(이름 없음)',
        title: comment.nickname || ''
      })
    ];
    if (isProtected(comment)) {
      nodes.push(
        el('div', { attrs: { style: 'margin-top:4px' } }, [
          TG.badge(
            '보호됨',
            'green',
            comment.is_admin
              ? '블로그 운영자가 작성한 댓글이라 삭제 대상에서 제외됩니다.'
              : '화이트리스트에 포함되어 삭제 대상에서 제외됩니다.'
          )
        ])
      );
    }
    return el('td', {}, nodes);
  }

  function buildRow(comment) {
    var locked = isProtected(comment);

    var checkbox = el('input', {
      className: 'app-check',
      attrs: {
        type: 'checkbox',
        'aria-label': (comment.nickname || '이름 없음') + ' 댓글 선택'
      }
    });
    checkbox.disabled = locked;
    checkbox.checked = !locked && state.selected.has(comment.comment_id);
    if (locked) {
      checkbox.title = '보호된 댓글은 선택할 수 없습니다.';
    }
    checkbox.addEventListener('change', function () {
      if (checkbox.checked) state.selected.add(comment.comment_id);
      else state.selected.delete(comment.comment_id);
      updateSelectionUi();
    });

    var entryTitle = targetTitles.get(comment.entry_id) || ('게시글 ' + comment.entry_id);
    var content = comment.content || '';

    return el(
      'tr',
      {
        dataset: { commentId: String(comment.comment_id) },
        attrs: { 'aria-selected': checkbox.checked ? 'true' : 'false' }
      },
      [
        el('td', { className: 'app-table__check' }, [checkbox]),
        nicknameCell(comment),
        el('td', {}, [
          el('span', {
            className: 'app-truncate app-cell-content',
            text: content || '(내용 없음)',
            title: content
          })
        ]),
        el('td', { className: 'app-nowrap', text: TG.formatDateTime(comment.written_at) }),
        el('td', {}, [
          el('a', {
            className: 'app-truncate app-cell-name',
            text: entryTitle,
            title: entryTitle + ' (글번호 ' + comment.entry_id + ')',
            attrs: { href: '/comments?entry_ids=' + encodeURIComponent(comment.entry_id) }
          })
        ]),
        el('td', {}, [spamBadge(comment)]),
        el('td', {}, [statusCell(comment)])
      ]
    );
  }

  function renderList(data) {
    state.items = (data && data.items) || [];
    state.total = (data && data.total) || 0;
    state.page = (data && data.page) || state.page;
    state.size = (data && data.size) || state.size;

    clear(refs.body);

    if (!state.items.length) {
      refs.body.appendChild(
        TG.tableMessageRow(
          COLSPAN,
          state.total
            ? '이 페이지에는 표시할 댓글이 없습니다. 페이지 번호를 확인하세요.'
            : '조건에 맞는 댓글이 없습니다. 필터를 넓히거나 게시글 관리에서 수집을 실행하세요.'
        )
      );
    } else {
      state.items.forEach(function (comment) {
        // 등급을 기억해 두면 손으로 고른 선택의 내역을 정확히 셀 수 있다.
        levelById.set(comment.comment_id, comment.spam_level || 'normal');
        refs.body.appendChild(buildRow(comment));
      });
    }

    var shown = state.items.length;
    refs.resultCount.textContent = '총 ' + TG.formatNumber(state.total) + '건 중 ' + TG.formatNumber(shown) + '건 표시';

    // summary 는 현재 페이지가 아니라 필터 조건 전체 기준이다.
    // selectable 이 화이트리스트를 뺀 실제 선택 대상 건수이므로 전체 선택 버튼에는 이 값을 쓴다.
    var summary = (data && data.summary) || null;
    if (summary) {
      refs.resultSummary.textContent =
        '필터 조건 전체 기준으로 선택 가능 ' + TG.formatNumber(summary.selectable || 0) +
        '건, 보호됨 ' + TG.formatNumber(summary.whitelisted || 0) + '건';
      refs.protectedHint.textContent = summary.whitelisted
        ? '보호된 댓글 ' + TG.formatNumber(summary.whitelisted) + '건은 삭제 대상에서 제외됩니다.'
        : '';
    } else {
      refs.resultSummary.textContent = '필터 조건에 맞는 댓글입니다.';
      refs.protectedHint.textContent = '';
    }

    var selectable = summary && summary.selectable !== undefined && summary.selectable !== null
      ? summary.selectable
      : state.total;
    refs.selectAllFiltered.textContent =
      '필터 결과 전체 선택 (' + TG.formatNumber(selectable) + '건)';
    refs.selectAllFiltered.disabled = !selectable;

    renderPager();
    updateSelectionUi();
  }

  function totalPages() {
    return Math.max(1, Math.ceil(state.total / (state.size || 50)));
  }

  function renderPager() {
    var pages = totalPages();
    var first = state.total ? (state.page - 1) * state.size + 1 : 0;
    var last = state.total ? Math.min(state.page * state.size, state.total) : 0;

    refs.pagerInfo.textContent = state.total
      ? TG.formatNumber(first) + '번째부터 ' + TG.formatNumber(last) + '번째까지, 총 ' +
        TG.formatNumber(state.total) + '건'
      : '표시할 댓글이 없습니다.';
    refs.pageInput.value = String(state.page);
    refs.pageInput.max = String(pages);
    refs.pageTotal.textContent = '/ ' + TG.formatNumber(pages);
    refs.pagePrev.disabled = state.page <= 1;
    refs.pageNext.disabled = state.page >= pages;
  }

  // ==========================================================================
  // 데이터 조회
  // ==========================================================================

  function load() {
    if (state.loading) return Promise.resolve();
    state.loading = true;

    // 필터가 바뀌면 이전 선택을 반드시 비운다.
    // 예: levels=spam 으로 5,383건을 고른 뒤 등급 칩을 "전체" 로 바꾸면 정상 댓글까지
    // 선택에 남아 삭제될 수 있다. 조건이 달라진 선택은 사용자가 확인한 선택이 아니다.
    var signature = filterSignature();
    if (state.filterSignature !== null && signature !== state.filterSignature) {
      var released = state.selected.size;
      if (released) {
        state.selected.clear();
        TG.toast(
          '필터 조건이 바뀌어 기존 선택 ' + TG.formatNumber(released) + '건을 해제했습니다. ' +
            '새 조건에서 다시 선택하세요.',
          'info'
        );
      }
    }
    state.filterSignature = signature;

    clear(refs.body);
    refs.body.appendChild(TG.tableMessageRow(COLSPAN, '댓글을 불러오는 중입니다.'));

    // 이 조회에 실제로 쓰인 조건을 남긴다. 전체 선택이 화면과 같은 집합을 고르게 한다.
    state.appliedParams = filterParams();

    var params = Object.assign({}, state.appliedParams, {
      page: state.page,
      size: state.size
    });
    syncUrl();

    return TG.api
      .get('/api/comments?' + TG.buildQuery(params))
      .then(renderList)
      .catch(function () {
        clear(refs.body);
        refs.body.appendChild(TG.tableMessageRow(COLSPAN, '댓글을 불러오지 못했습니다.'));
      })
      .then(function () {
        state.loading = false;
      });
  }

  /**
   * 삭제 전 백업 여부는 .env 의 APP_BACKUP_BEFORE_DELETE 가 정한다.
   * 확인 모달이 사실과 다른 보장을 표시하지 않도록 실제 값을 읽어 둔다.
   */
  function loadBackupSetting() {
    return TG.api
      .get('/api/settings', { silent: true })
      .then(function (data) {
        var runtime = (data && data.runtime) || {};
        state.backupBeforeDelete =
          runtime.backup_before_delete === undefined ? null : !!runtime.backup_before_delete;
      })
      .catch(function () {
        // 확인하지 못했으면 "백업된다" 고 단정하지 않는다.
        state.backupBeforeDelete = null;
      });
  }

  function loadTargets() {
    return TG.api
      .get('/api/targets', { silent: true })
      .then(function (data) {
        state.targets = (data && data.items) || [];
        targetTitles = new Map();
        state.targets.forEach(function (target) {
          targetTitles.set(target.entry_id, target.title || ('게시글 ' + target.entry_id));
        });
        buildEntryChips(state.targets);
      })
      .catch(function () {
        clear(refs.entryChips);
        refs.entryChips.appendChild(
          el('span', {
            className: 'app-muted-soft app-text-sm',
            text: '게시글 목록을 불러오지 못했습니다. 게시글 필터 없이 조회합니다.'
          })
        );
      });
  }

  // ==========================================================================
  // 필터 동작
  // ==========================================================================

  refs.form.addEventListener('submit', function (event) {
    event.preventDefault();
    state.page = 1;
    load();
  });

  refs.resetBtn.addEventListener('click', function () {
    state.entryIds = [];
    state.levels = [];
    state.statuses = ['active'];
    refs.dateFrom.value = '';
    refs.dateTo.value = '';
    refs.nickname.value = '';
    refs.content.value = '';
    refs.minScore.value = '';
    state.page = 1;
    paintEntryChips();
    paintLevelChips();
    paintStatusChips();
    load();
  });

  refs.pagePrev.addEventListener('click', function () {
    if (state.page <= 1) return;
    state.page -= 1;
    load();
  });

  refs.pageNext.addEventListener('click', function () {
    if (state.page >= totalPages()) return;
    state.page += 1;
    load();
  });

  refs.pageForm.addEventListener('submit', function (event) {
    event.preventDefault();
    var value = parseInt(refs.pageInput.value, 10);
    if (!isFinite(value) || value < 1) value = 1;
    var pages = totalPages();
    if (value > pages) value = pages;
    state.page = value;
    load();
  });

  // ==========================================================================
  // 선택 동작
  // ==========================================================================

  refs.selectPage.addEventListener('change', function () {
    var checked = refs.selectPage.checked;
    state.items.forEach(function (comment) {
      if (isProtected(comment)) return;
      if (checked) state.selected.add(comment.comment_id);
      else state.selected.delete(comment.comment_id);
    });
    updateSelectionUi();
  });

  refs.clearSelection.addEventListener('click', function () {
    state.selected.clear();
    updateSelectionUi();
  });

  refs.selectAllFiltered.addEventListener('click', function () {
    TG.guard(refs.selectAllFiltered, function () {
      return TG.api
        // 화면에 표시된 목록과 같은 조건으로 고른다. 아직 적용하지 않은 칩 변경은 쓰지 않는다.
        .post('/api/comments/select-ids', { filter: state.appliedParams })
        .then(function (data) {
          var ids = (data && data.ids) || [];
          var levels = (data && data.levels) || [];
          ids.forEach(function (id, index) {
            state.selected.add(id);
            // 등급을 함께 기억해 둬야 이후 체크를 해제했을 때 등급별 내역이 바로 맞는다.
            if (levels[index]) levelById.set(id, levels[index]);
          });
          updateSelectionUi();

          var excluded = (data && data.whitelisted_excluded) || 0;
          TG.toast(
            '필터 결과 ' + TG.formatNumber(ids.length) + '건을 선택했습니다.' +
              (excluded ? ' 보호된 ' + TG.formatNumber(excluded) + '건은 제외했습니다.' : ''),
            'success'
          );
        });
    }).catch(function () {});
  });

  // ==========================================================================
  // 재분류
  // ==========================================================================

  refs.rescoreBtn.addEventListener('click', function () {
    var entryIds = state.entryIds.slice();
    TG.confirmModal({
      title: '스팸 점수를 다시 계산할까요?',
      body:
        (entryIds.length
          ? '선택한 게시글 ' + TG.formatNumber(entryIds.length) + '개의 댓글을 다시 분류합니다.'
          : '수집된 모든 댓글을 다시 분류합니다.') +
        '\n설정 화면에서 규칙을 고친 뒤에 실행하세요. 블로그에는 아무 요청도 보내지 않습니다.',
      confirmText: '재분류 실행'
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.rescoreBtn, function () {
        var payload = entryIds.length ? { entry_ids: entryIds } : {};
        return TG.api.post('/api/comments/rescore', payload).then(function (data) {
          TG.toast(
            '댓글 ' + TG.formatNumber((data && data.updated) || 0) + '건의 점수를 갱신했습니다.',
            'success'
          );
          return load();
        });
      }).catch(function () {});
    });
  });

  // ==========================================================================
  // 백업과 삭제
  // ==========================================================================

  var LEVEL_ORDER = ['spam', 'suspicious', 'normal'];

  /**
   * 선택한 댓글의 등급별 내역을 구한다. 건수 하나만으로는 5,383 과 5,392 를 구별할 수 없어서
   * 정상 등급이 섞여 들어간 것을 사용자가 알아챌 수 없기 때문이다.
   *
   * 선택은 두 경로로만 들어오고 두 경로 모두 등급을 알려 준다.
   *   - 화면의 체크박스: 목록을 그릴 때 levelById 에 등급을 기억해 둔다.
   *   - 필터 결과 전체 선택: 서버가 ids 와 같은 순서의 levels 를 함께 내려준다.
   * 그래서 선택 집합만 보고 정확히 셀 수 있다. 예전처럼 필터 조건으로 다시 세면
   * 체크를 해제한 항목이 반영되지 않아 "해제했는데 숫자가 그대로" 가 된다.
   */
  function gradeBreakdown() {
    var counts = { spam: 0, suspicious: 0, normal: 0 };
    var unknown = 0;

    state.selected.forEach(function (id) {
      var level = levelById.get(id);
      if (!level) {
        unknown += 1;
        return;
      }
      // 서버가 새 등급을 추가해도 화면이 조용히 스팸으로 세지 않도록 모르는 값은 따로 센다.
      if (counts[level] === undefined) unknown += 1;
      else counts[level] += 1;
    });

    return { counts: counts, unknown: unknown };
  }

  var LEVEL_LABEL = { spam: '스팸', suspicious: '의심', normal: '정상' };

  /** 삭제 확인 모달의 본문을 만든다. */
  function buildDeleteBody(count, dryRun, breakdown) {
    var facts = el('dl', { className: 'app-modal__facts' }, [
      el('dt', { text: '대상 건수' }),
      el('dd', { text: TG.formatNumber(count) + '건' }),
      el('dt', { text: '실행 방식' }),
      el('dd', { text: dryRun ? '드라이런 (실제 삭제 없음)' : '실제 삭제' })
    ]);

    // 등급별 내역. 선택 집합을 그대로 센 값이라 체크를 해제하면 즉시 반영된다.
    var counts = (breakdown && breakdown.counts) || {};
    var unknown = (breakdown && breakdown.unknown) || 0;
    var parts = [];
    LEVEL_ORDER.forEach(function (level) {
      parts.push(LEVEL_LABEL[level] + ' ' + TG.formatNumber(counts[level] || 0) + '건');
    });
    if (unknown) parts.push('등급 확인 불가 ' + TG.formatNumber(unknown) + '건');
    facts.appendChild(el('dt', { text: '등급별 내역' }));
    facts.appendChild(el('dd', { text: parts.join(',  ') }));

    facts.appendChild(el('dt', { text: '백업' }));
    facts.appendChild(
      el('dd', {
        text: dryRun
          ? '만들지 않습니다. 파일이 필요하면 백업 내보내기를 쓰세요.'
          : state.backupBeforeDelete === true
            ? '삭제 전에 JSON 과 CSV 백업 파일을 만듭니다.'
            : state.backupBeforeDelete === false
              ? '만들지 않습니다.'
              : '설정을 확인하지 못했습니다.'
      })
    );

    var nodes = [facts];

    // 정상 등급이 섞여 있으면 가장 먼저 눈에 띄게 알린다.
    if (counts.normal) {
      nodes.push(
        el('p', {
          className: 'app-modal__warn',
          text:
            '스팸으로 분류되지 않은 정상 등급 댓글 ' + TG.formatNumber(counts.normal) +
            '건이 대상에 들어 있습니다. 진짜 독자가 남긴 댓글일 수 있으니 목록에서 내용을 먼저 확인하세요.'
        })
      );
    }

    if (unknown) {
      nodes.push(
        el('p', {
          className: 'app-modal__warn',
          text:
            '등급을 확인하지 못한 댓글이 ' + TG.formatNumber(unknown) +
            '건 있습니다. 선택을 해제하고 목록에서 다시 고르세요.'
        })
      );
    }

    if (dryRun) {
      nodes.push(
        el('p', {
          className: 'app-modal__note',
          text:
            '드라이런은 블로그에 삭제 요청을 보내지 않습니다. 대상 선정 절차만 검증하고 결과를 작업 이력에 남기며, ' +
            '실제로 지우지 않으므로 백업 파일도 만들지 않습니다. 파일이 필요하면 백업 내보내기 버튼을 사용하세요.'
        })
      );
    } else {
      nodes.push(
        el('p', {
          className: 'app-modal__warn',
          text:
            '실제 삭제는 되돌릴 수 없습니다. 대량 삭제 전에 설정 화면에서 1건 시험 삭제로 세션이 정상인지 먼저 확인하세요.'
        })
      );

      // 백업 보장은 서버 설정에 달려 있다. 사실과 다른 안내를 하지 않는다.
      if (state.backupBeforeDelete === true) {
        nodes.push(
          el('p', {
            text: '삭제 전에 백업 파일을 만들며, 백업 생성에 실패하면 삭제를 시작하지 않습니다.'
          })
        );
      } else if (state.backupBeforeDelete === false) {
        nodes.push(
          el('p', {
            className: 'app-modal__warn',
            text:
              '이번 삭제는 백업을 만들지 않습니다. .env 의 APP_BACKUP_BEFORE_DELETE 가 꺼져 있어 ' +
              '삭제한 댓글을 복구할 수단이 남지 않습니다. 먼저 백업 내보내기로 파일을 만들어 두세요.'
          })
        );
      } else {
        nodes.push(
          el('p', {
            className: 'app-modal__warn',
            text:
              '백업 설정을 확인하지 못했습니다. 백업이 만들어진다고 보장할 수 없으니 ' +
              '먼저 백업 내보내기로 파일을 만들어 두세요.'
          })
        );
      }
    }

    return el('div', {}, nodes);
  }

  /** 작업 결과 요약 카드를 그린다. */
  function renderJobResult(payload, backup, dryRun) {
    clear(refs.result);
    if (!payload && !backup) return;

    var rows = [];
    if (payload) {
      rows.push(
        el('div', { className: 'app-kv__item' }, [
          el('span', { className: 'app-kv__key', text: '성공' }),
          el('span', { className: 'app-kv__val', text: TG.formatNumber(payload.succeeded || 0) + '건' })
        ])
      );
      rows.push(
        el('div', { className: 'app-kv__item' }, [
          el('span', { className: 'app-kv__key', text: '실패' }),
          el('span', { className: 'app-kv__val', text: TG.formatNumber(payload.failed || 0) + '건' })
        ])
      );
      rows.push(
        el('div', { className: 'app-kv__item' }, [
          el('span', { className: 'app-kv__key', text: '건너뜀' }),
          el('span', { className: 'app-kv__val', text: TG.formatNumber(payload.skipped || 0) + '건' })
        ])
      );
    }

    var links = [];
    if (backup && backup.json) links.push(backupLink('JSON 백업 내려받기', backup.json));
    if (backup && backup.csv) links.push(backupLink('CSV 백업 내려받기', backup.csv));

    var card = el('div', { className: 'app-card' }, [
      el('div', { className: 'app-card__head' }, [
        el('div', {}, [
          el('h2', {
            className: 'app-card__title',
            text: dryRun ? '드라이런 결과' : '삭제 결과'
          }),
          el('p', {
            className: 'app-card__desc',
            text: payload && payload.job_id
              ? '작업 ' + payload.job_id + '번. 자세한 항목별 결과는 작업 이력에서 확인할 수 있습니다.'
              : '자세한 항목별 결과는 작업 이력에서 확인할 수 있습니다.'
          })
        ]),
        el('div', { className: 'app-card__actions' }, [
          el('a', {
            className: 'app-btn app-btn--secondary app-btn--sm',
            text: '작업 이력 열기',
            attrs: { href: '/jobs' }
          })
        ])
      ]),
      rows.length ? el('div', { className: 'app-kv' }, rows) : null,
      links.length ? el('div', { className: 'app-row', attrs: { style: 'margin-top:12px' } }, links) : null
    ]);

    refs.result.appendChild(card);
  }

  /** 서버가 준 경로에서 파일 이름을 뽑아 다운로드 링크를 만든다. (삭제 작업의 backup 필드용) */
  function backupLink(label, path) {
    return backupFileLink(label, TG.baseName(path));
  }

  /** 파일 이름을 그대로 받아 다운로드 링크를 만든다. (내보내기 응답용) */
  function backupFileLink(label, name) {
    return el('a', {
      className: 'app-btn app-btn--secondary app-btn--sm',
      text: label,
      title: name,
      attrs: { href: '/api/backups/' + encodeURIComponent(name), download: name }
    });
  }

  /**
   * 삭제 작업을 생성한다.
   * 계약상 comment_ids 와 filter 중 하나만 지정해야 하므로, 사용자가 확인 모달에서 본
   * 건수와 정확히 일치하도록 항상 ID 목록을 보낸다.
   *
   * allowNormal 은 정상 등급 댓글까지 지울지 여부다. 항상 false 로 먼저 시도해 서버의
   * 안전장치를 통과시키고, 사용자가 별도 확인을 마친 경우에만 true 로 재요청한다.
   */
  function createDeleteJob(commentIds, dryRun, allowNormal) {
    return TG.api.post(
      '/api/jobs/delete',
      {
        comment_ids: commentIds,
        filter: null,
        dry_run: dryRun,
        allow_normal: !!allowNormal
      },
      // 1차 시도의 400 은 별도 확인 단계로 처리하므로 토스트로 흘려보내지 않는다.
      { silent: !allowNormal }
    );
  }

  /** 정상 등급이 섞여 서버가 거부한 응답인지 판별한다. */
  function isNormalMixedError(error) {
    if (!error || error.status !== 400) return false;
    var message = error.message || '';
    return /스팸으로 분류되지 않은|정상 등급/.test(message);
  }

  /**
   * 정상 등급 포함 여부를 다시 묻는다.
   * 서버가 보낸 문구를 가공하지 않고 그대로 보여주고, 체크박스를 켠 경우에만 진행한다.
   */
  function confirmAllowNormal(serverMessage, dryRun) {
    var body = el('div', {}, [
      el('p', {
        className: 'app-modal__warn',
        // 서버 메시지에는 댓글 번호가 들어 있다. textContent 로만 넣는다.
        text: serverMessage
      }),
      el('p', {
        text:
          '도배 정리 중에 진짜 독자가 남긴 댓글이 딸려 들어가는 것을 막기 위한 확인 단계입니다. ' +
          '해당 댓글의 내용을 목록에서 직접 확인한 뒤에 진행하세요.'
      }),
      el('p', {
        className: 'app-muted app-text-sm',
        text: dryRun
          ? '드라이런에서도 대상 선정 방식은 실제 삭제와 같으므로 같은 확인을 거칩니다.'
          : '이 작업은 되돌릴 수 없습니다.'
      })
    ]);

    return TG.confirmModal({
      title: '정상 등급 댓글이 대상에 섞여 있습니다.',
      body: body,
      confirmText: dryRun ? '포함하고 드라이런 실행' : '포함하고 삭제 실행',
      danger: true,
      requireCheckbox: '정상 등급 댓글도 포함해 삭제합니다.'
    });
  }

  function handleJobStarted(data, dryRun, label) {
    var jobId = data && data.job_id;
    var backup = data && data.backup;
    renderJobResult(null, backup, dryRun);

    if (!jobId && jobId !== 0) {
      TG.toast('작업 번호를 받지 못했습니다. 작업 이력 화면에서 상태를 확인하세요.', 'error');
      return;
    }

    progressPanel.track(jobId, {
      label: label,
      onDone: function (payload) {
        var status = (payload && payload.status) || 'completed';
        renderJobResult(Object.assign({ job_id: jobId }, payload || {}), backup, dryRun);
        if (status === 'completed') {
          TG.toast(label + '이(가) 완료되었습니다.', 'success');
        } else {
          TG.toast(label + '이(가) ' + (TG.JOB_STATUS_LABEL[status] || status) + ' 상태로 끝났습니다.', 'error');
        }
        if (!dryRun) {
          state.selected.clear();
          load();
        }
      }
    });
  }

  refs.deleteBtn.addEventListener('click', function () {
    var ids = Array.from(state.selected);
    if (!ids.length) return;
    var dryRun = refs.dryRun.checked;
    var label = dryRun ? '드라이런 삭제 검증' : '댓글 삭제';

    // 등급 내역은 선택 집합만으로 즉시 계산된다. 서버에 다시 묻지 않는다.
    TG.confirmModal({
      title: dryRun ? '드라이런으로 삭제 절차를 확인할까요?' : '선택한 댓글을 삭제할까요?',
      body: buildDeleteBody(ids.length, dryRun, gradeBreakdown()),
      confirmText: dryRun ? '드라이런 실행' : '삭제 실행',
      danger: !dryRun,
      // 드라이런은 블로그를 바꾸지 않으므로 문구 입력을 요구하지 않는다.
      requireTyping: dryRun ? null : '삭제'
    })
      .then(function (ok) {
        if (!ok) return undefined;
        return TG.guard(refs.deleteBtn, function () {
          return runDelete(ids, dryRun, label);
        });
      })
      .catch(function () {});
  });

  /** 1차 요청 후 정상 등급 거부를 만나면 확인 단계를 거쳐 재요청한다. */
  function runDelete(ids, dryRun, label) {
    return createDeleteJob(ids, dryRun, false)
      .then(function (data) {
        handleJobStarted(data, dryRun, label);
      })
      .catch(function (error) {
        if (!isNormalMixedError(error)) {
          // silent 로 보냈으므로 그 밖의 오류는 여기서 직접 알린다.
          TG.toast(error.message, 'error');
          throw error;
        }
        return confirmAllowNormal(error.message, dryRun).then(function (ok) {
          if (!ok) {
            TG.toast('정상 등급 댓글이 포함되어 있어 실행하지 않았습니다.', 'info');
            return undefined;
          }
          return createDeleteJob(ids, dryRun, true).then(function (data) {
            handleJobStarted(data, dryRun, label);
          });
        });
      });
  }

  /**
   * 내보내기 결과 카드를 그린다.
   * POST /api/backups/export 는 경로가 아니라 파일 이름을 돌려주므로 그대로 다운로드 링크에 넣는다.
   */
  function renderExportResult(data) {
    clear(refs.result);
    if (!data) return;

    var links = [];
    if (data.json_file) links.push(backupFileLink('JSON 파일 내려받기', data.json_file));
    if (data.csv_file) links.push(backupFileLink('CSV 파일 내려받기', data.csv_file));

    refs.result.appendChild(
      el('div', { className: 'app-card' }, [
        el('div', { className: 'app-card__head' }, [
          el('div', {}, [
            el('h2', { className: 'app-card__title', text: '백업 내보내기 결과' }),
            el('p', {
              className: 'app-card__desc',
              text:
                TG.formatNumber(data.count || 0) + '건을 저장했습니다. 생성 시각 ' +
                TG.formatDateTime(data.created_at)
            })
          ]),
          el('div', { className: 'app-card__actions' }, [
            el('a', {
              className: 'app-btn app-btn--secondary app-btn--sm',
              text: '백업 목록 열기',
              attrs: { href: '/settings' }
            })
          ])
        ]),
        links.length ? el('div', { className: 'app-row' }, links) : null
      ])
    );
  }

  refs.backupBtn.addEventListener('click', function () {
    var ids = Array.from(state.selected);
    if (!ids.length) return;

    TG.confirmModal({
      title: '선택한 댓글을 백업 파일로 내보낼까요?',
      body:
        '선택한 ' + TG.formatNumber(ids.length) + '건을 JSON 과 CSV 파일로 저장합니다.\n' +
        '삭제 작업을 만들지 않고 파일만 생성하므로 블로그에는 아무 요청도 보내지 않습니다.\n' +
        '생성된 파일은 이 화면과 설정 화면의 백업 목록에서 내려받을 수 있습니다.',
      confirmText: '백업 만들기'
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.backupBtn, function () {
        return TG.api
          .post('/api/backups/export', {
            comment_ids: ids,
            filter: null,
            label: 'export'
          })
          .then(function (data) {
            renderExportResult(data);
            TG.toast(
              '백업 ' + TG.formatNumber((data && data.count) || 0) + '건을 저장했습니다.',
              'success'
            );
          });
      }).catch(function () {});
    });
  });

  // ==========================================================================
  // 시작
  // ==========================================================================

  readUrl();
  paintLevelChips();
  paintStatusChips();
  updateSelectionUi();
  loadBackupSetting();
  loadTargets().then(load);
})();
