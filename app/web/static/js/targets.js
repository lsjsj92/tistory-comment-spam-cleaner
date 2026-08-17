// app/web/static/js/targets.js
//
// 게시글 관리 화면.
// 호출 API
//   GET    /api/targets
//   POST   /api/targets              { url_or_id }
//   PATCH  /api/targets/{entry_id}   { enabled }
//   DELETE /api/targets/{entry_id}
//   POST   /api/targets/discover
//   POST   /api/targets/collect      { entry_ids, since }
//   GET    /api/jobs/{id}/stream     (core.js 의 진행률 패널이 사용)

(function () {
  'use strict';

  var el = TG.el;
  var clear = TG.clear;
  var COLSPAN = 9;

  var refs = {
    body: document.getElementById('app-targets-body'),
    count: document.getElementById('app-targets-count'),
    selectAll: document.getElementById('app-targets-select-all'),
    addForm: document.getElementById('app-target-add-form'),
    addInput: document.getElementById('app-target-input'),
    addBtn: document.getElementById('app-target-add-btn'),
    discoverBtn: document.getElementById('app-targets-discover'),
    refreshBtn: document.getElementById('app-targets-refresh'),
    collectBtn: document.getElementById('app-collect-btn'),
    collectHint: document.getElementById('app-collect-hint'),
    since: document.getElementById('app-collect-since'),
    progress: document.getElementById('app-targets-progress')
  };

  var progressPanel = TG.jobProgressPanel(refs.progress);

  var state = {
    items: [],
    selected: new Set()
  };

  // ==========================================================================
  // 렌더링
  // ==========================================================================

  function updateCollectHint() {
    var count = state.selected.size;
    refs.collectHint.textContent = count
      ? '선택한 게시글 ' + TG.formatNumber(count) + '개를 수집합니다.'
      : '선택한 게시글이 없으면 활성 게시글 전체를 수집합니다.';

    var visible = state.items.length;
    refs.selectAll.checked = visible > 0 && count === visible;
    refs.selectAll.indeterminate = count > 0 && count < visible;
  }

  function buildRow(target) {
    var entryId = target.entry_id;
    var title = target.title || ('게시글 ' + entryId);

    var checkbox = el('input', {
      className: 'app-check',
      attrs: {
        type: 'checkbox',
        'aria-label': title + ' 선택'
      }
    });
    checkbox.checked = state.selected.has(entryId);
    checkbox.addEventListener('change', function () {
      if (checkbox.checked) state.selected.add(entryId);
      else state.selected.delete(entryId);
      row.setAttribute('aria-selected', checkbox.checked ? 'true' : 'false');
      updateCollectHint();
    });

    var toggleInput = el('input', {
      className: 'app-switch__input',
      attrs: { type: 'checkbox', 'aria-label': title + ' 수집 활성화' }
    });
    toggleInput.checked = !!target.enabled;
    toggleInput.addEventListener('change', function () {
      var next = toggleInput.checked;
      toggleInput.disabled = true;
      TG.api
        .patch('/api/targets/' + encodeURIComponent(entryId), { enabled: next })
        .then(function () {
          target.enabled = next;
          TG.toast(title + ' 게시글을 ' + (next ? '활성화' : '비활성화') + '했습니다.', 'success');
        })
        .catch(function () {
          toggleInput.checked = !next;
        })
        .then(function () {
          toggleInput.disabled = false;
        });
    });

    var deleteBtn = el('button', {
      className: 'app-btn app-btn--secondary app-btn--sm',
      text: '삭제',
      attrs: { type: 'button' }
    });
    deleteBtn.addEventListener('click', function () {
      removeTarget(target, deleteBtn);
    });

    var row = el('tr', { attrs: { 'aria-selected': checkbox.checked ? 'true' : 'false' } }, [
      el('td', { className: 'app-table__check' }, [checkbox]),
      el('td', { className: 'app-num', text: String(entryId) }),
      el('td', {}, [
        el('div', { className: 'app-cell-title app-truncate app-cell-content', text: title, title: title })
      ]),
      el('td', {}, [
        target.url
          ? el('a', {
              className: 'app-truncate app-cell-content',
              text: target.url,
              title: target.url,
              attrs: { href: target.url, target: '_blank', rel: 'noopener noreferrer' }
            })
          : el('span', { className: 'app-muted-soft', text: '-' })
      ]),
      el('td', { className: 'app-num', text: TG.formatNumber(target.comment_count) }),
      el('td', { className: 'app-nowrap', text: TG.formatDateTime(target.last_collected_at) }),
      el('td', {}, [
        TG.badge(target.source === 'sitemap' ? 'sitemap' : '직접 등록', 'gray')
      ]),
      el('td', {}, [
        el('label', { className: 'app-switch' }, [
          toggleInput,
          el('span', { className: 'app-switch__track' })
        ])
      ]),
      el('td', { className: 'app-nowrap' }, [
        el('div', { className: 'app-row' }, [
          el('a', {
            className: 'app-btn app-btn--secondary app-btn--sm',
            text: '댓글 보기',
            attrs: { href: '/comments?entry_ids=' + encodeURIComponent(entryId) }
          }),
          deleteBtn
        ])
      ])
    ]);

    return row;
  }

  function render() {
    clear(refs.body);

    if (!state.items.length) {
      refs.body.appendChild(
        TG.tableMessageRow(
          COLSPAN,
          '등록된 게시글이 없습니다. 위에서 주소나 글번호를 추가하거나 sitemap 전체 스캔을 실행하세요.'
        )
      );
      refs.count.textContent = '등록된 게시글이 없습니다.';
      updateCollectHint();
      return;
    }

    state.items.forEach(function (target) {
      refs.body.appendChild(buildRow(target));
    });

    var enabled = state.items.filter(function (item) {
      return item.enabled;
    }).length;
    refs.count.textContent =
      '총 ' + TG.formatNumber(state.items.length) + '개, 활성 ' + TG.formatNumber(enabled) + '개';
    updateCollectHint();
  }

  // ==========================================================================
  // 데이터
  // ==========================================================================

  function load() {
    clear(refs.body);
    refs.body.appendChild(TG.tableMessageRow(COLSPAN, '게시글 목록을 불러오는 중입니다.'));

    return TG.api
      .get('/api/targets')
      .then(function (data) {
        state.items = (data && data.items) || [];
        // 목록이 바뀌면 사라진 게시글의 선택은 정리한다.
        var alive = new Set(
          state.items.map(function (item) {
            return item.entry_id;
          })
        );
        Array.from(state.selected).forEach(function (id) {
          if (!alive.has(id)) state.selected.delete(id);
        });
        render();
      })
      .catch(function () {
        clear(refs.body);
        refs.body.appendChild(TG.tableMessageRow(COLSPAN, '목록을 불러오지 못했습니다.'));
        refs.count.textContent = '목록을 불러오지 못했습니다.';
      });
  }

  function removeTarget(target, button) {
    var title = target.title || ('게시글 ' + target.entry_id);
    TG.confirmModal({
      title: '게시글을 목록에서 제거할까요?',
      body:
        title +
        ' 을(를) 수집 대상에서 제거합니다.\n이미 수집한 댓글 데이터는 그대로 남습니다.',
      confirmText: '제거',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(button, function () {
        return TG.api.del('/api/targets/' + encodeURIComponent(target.entry_id)).then(function () {
          TG.toast('게시글을 제거했습니다.', 'success');
          state.selected.delete(target.entry_id);
          return load();
        });
      }).catch(function () {
        /* 오류 토스트는 api 래퍼가 이미 띄웠다 */
      });
    });
  }

  // ==========================================================================
  // 동작
  // ==========================================================================

  refs.addForm.addEventListener('submit', function (event) {
    event.preventDefault();
    var value = refs.addInput.value.trim();
    if (!value) {
      TG.toast('게시글 주소나 글번호를 입력하세요.', 'error');
      refs.addInput.focus();
      return;
    }
    TG.guard(refs.addBtn, function () {
      return TG.api.post('/api/targets', { url_or_id: value }).then(function (data) {
        var item = data && data.item;
        TG.toast(
          item ? '게시글 ' + item.entry_id + '번을 등록했습니다.' : '게시글을 등록했습니다.',
          'success'
        );
        refs.addInput.value = '';
        return load();
      });
    }).catch(function () {});
  });

  refs.refreshBtn.addEventListener('click', function () {
    TG.guard(refs.refreshBtn, load);
  });

  refs.selectAll.addEventListener('change', function () {
    if (refs.selectAll.checked) {
      state.items.forEach(function (item) {
        state.selected.add(item.entry_id);
      });
    } else {
      state.selected.clear();
    }
    render();
  });

  refs.discoverBtn.addEventListener('click', function () {
    TG.confirmModal({
      title: 'sitemap 전체 스캔을 실행할까요?',
      body:
        '블로그의 sitemap.xml 을 읽어 게시글을 찾아 목록에 등록합니다.\n' +
        '게시글 수에 따라 시간이 걸릴 수 있으며, 댓글 수집은 별도로 실행해야 합니다.',
      confirmText: '스캔 시작'
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.discoverBtn, function () {
        return TG.api.post('/api/targets/discover', {}).then(function (data) {
          startJob(data && data.job_id, 'sitemap 전체 스캔');
        });
      }).catch(function () {});
    });
  });

  refs.collectBtn.addEventListener('click', function () {
    var entryIds = Array.from(state.selected);
    var since = refs.since.value.trim();

    var lines = [];
    lines.push(
      entryIds.length
        ? '선택한 게시글 ' + TG.formatNumber(entryIds.length) + '개의 댓글을 수집합니다.'
        : '활성화된 게시글 전체의 댓글을 수집합니다.'
    );
    lines.push(
      since
        ? '수집 시작 시각: ' + since + ' (이 시각보다 과거로 내려가면 중단합니다.)'
        : '기간을 지정하지 않아 게시글의 모든 댓글을 수집합니다.'
    );
    lines.push('수집은 블로그를 변경하지 않으며 읽기만 수행합니다.');

    TG.confirmModal({
      title: '댓글 수집을 시작할까요?',
      body: lines.join('\n'),
      confirmText: '수집 시작'
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.collectBtn, function () {
        var payload = { entry_ids: entryIds };
        if (since) payload.since = since;
        return TG.api.post('/api/targets/collect', payload).then(function (data) {
          startJob(
            data && data.job_id,
            '댓글 수집 (' + TG.formatNumber((data && data.total) || entryIds.length) + '개 게시글)'
          );
        });
      }).catch(function () {});
    });
  });

  /** 작업 ID 를 받아 진행률 패널을 붙이고 완료 시 목록을 새로 고친다. */
  function startJob(jobId, label) {
    if (!jobId && jobId !== 0) {
      TG.toast('작업 번호를 받지 못했습니다. 작업 이력 화면에서 상태를 확인하세요.', 'error');
      return;
    }
    TG.toast(label + ' 작업(' + jobId + '번)을 시작했습니다.', 'info');
    progressPanel.track(jobId, {
      label: label,
      onDone: function (payload) {
        var status = (payload && payload.status) || 'completed';
        TG.toast(
          status === 'completed' ? label + ' 작업이 완료되었습니다.' : label + ' 작업이 ' + status + ' 상태로 끝났습니다.',
          status === 'completed' ? 'success' : 'error'
        );
        load();
      }
    });
  }

  load();
})();
