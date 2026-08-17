// app/web/static/js/dashboard.js
//
// 대시보드 화면. 호출하는 API 는 GET /api/stats/overview 하나뿐이다.

(function () {
  'use strict';

  var el = TG.el;
  var clear = TG.clear;

  var TARGET_COLSPAN = 9;

  var refs = {
    stats: document.getElementById('app-dash-stats'),
    empty: document.getElementById('app-dash-empty'),
    charts: document.getElementById('app-dash-charts'),
    histogram: document.getElementById('app-dash-histogram'),
    nicknames: document.getElementById('app-dash-nicknames'),
    targets: document.getElementById('app-dash-targets'),
    running: document.getElementById('app-dash-running'),
    runningText: document.getElementById('app-dash-running-text'),
    errorBox: document.getElementById('app-dash-error'),
    refresh: document.getElementById('app-dash-refresh')
  };

  /** 스탯 타일 숫자를 갱신한다. 단위 span 은 유지한다. */
  function setStat(name, value) {
    var node = refs.stats.querySelector('[data-stat="' + name + '"]');
    if (!node) return;
    var unit = node.querySelector('.app-stat__unit');
    node.textContent = TG.formatNumber(value);
    if (unit) node.appendChild(unit);
  }

  function renderTargets(targets) {
    var body = refs.targets;
    clear(body);

    if (!targets.length) {
      body.appendChild(TG.tableMessageRow(TARGET_COLSPAN, '등록된 게시글이 없습니다.'));
      return;
    }

    targets.forEach(function (target) {
      var title = target.title || ('게시글 ' + target.entry_id);
      var commentsHref = '/comments?entry_ids=' + encodeURIComponent(target.entry_id);

      body.appendChild(
        el('tr', {}, [
          el('td', {}, [
            el('div', { className: 'app-cell-title app-truncate', text: title, title: title }),
            el('div', { className: 'app-cell-sub', text: '글번호 ' + target.entry_id })
          ]),
          el('td', { className: 'app-num', text: TG.formatNumber(target.total) }),
          el('td', { className: 'app-num', text: TG.formatNumber(target.active) }),
          el('td', { className: 'app-num', text: TG.formatNumber(target.deleted) }),
          el('td', { className: 'app-num' }, [
            target.spam ? TG.badge(TG.formatNumber(target.spam), 'red') : el('span', { text: '0' })
          ]),
          el('td', { className: 'app-num' }, [
            target.suspicious
              ? TG.badge(TG.formatNumber(target.suspicious), 'amber')
              : el('span', { text: '0' })
          ]),
          el('td', { className: 'app-nowrap', text: TG.formatDateTime(target.first_written_at) }),
          el('td', { className: 'app-nowrap', text: TG.formatDateTime(target.last_written_at) }),
          el('td', { className: 'app-nowrap' }, [
            el('a', {
              className: 'app-btn app-btn--secondary app-btn--sm',
              text: '댓글 보기',
              attrs: { href: commentsHref }
            })
          ])
        ])
      );
    });
  }

  function renderCharts(data) {
    var histogram = (data.histogram || []).map(function (row) {
      return { label: row.bucket, value: row.count };
    });
    TGChart.renderBarChart(refs.histogram, histogram, {
      height: 250,
      valueUnit: '건',
      ariaLabel: '시간대별 댓글 유입 막대 차트',
      emptyText: '표시할 유입 기록이 없습니다.'
    });

    var nicknames = (data.top_nicknames || []).map(function (row) {
      // 닉네임 없이 등록된 댓글이 실제로 존재한다. 빈 막대로 보이지 않게 표시명을 채운다.
      return { label: row.nickname || '(이름 없음)', value: row.count };
    });
    TGChart.renderHorizontalBars(refs.nicknames, nicknames, {
      rowHeight: 30,
      maxItems: 10,
      valueUnit: '건',
      ariaLabel: '상위 작성자 막대 차트',
      emptyText: '표시할 작성자가 없습니다.'
    });
  }

  function render(data) {
    var totals = data.totals || {};
    var targets = data.targets || [];

    setStat('total', totals.total || 0);
    setStat('spam', totals.spam || 0);
    setStat('suspicious', totals.suspicious || 0);
    setStat('deleted', totals.deleted || 0);
    setStat('failed', totals.failed || 0);
    // 서버가 totals.targets 를 주면 그 값을 쓰고, 없으면 목록 길이로 대신한다.
    setStat('targets', totals.targets !== undefined && totals.targets !== null
      ? totals.targets
      : targets.length);

    var isEmpty = !(totals.total || 0);
    refs.empty.classList.toggle('app-hidden', !isEmpty);
    refs.charts.classList.toggle('app-hidden', isEmpty);

    var running = Number(data.running_jobs || 0);
    refs.running.classList.toggle('app-hidden', running <= 0);
    if (running > 0) {
      refs.runningText.textContent =
        '실행 중인 작업이 ' + TG.formatNumber(running) + '건 있습니다. 진행 상황은 작업 이력에서 확인하세요.';
    }

    renderTargets(targets);
    if (!isEmpty) renderCharts(data);
  }

  function showLoading() {
    clear(refs.errorBox);
    clear(refs.targets);
    refs.targets.appendChild(
      TG.tableMessageRow(TARGET_COLSPAN, '게시글 현황을 불러오는 중입니다.')
    );
  }

  function load() {
    showLoading();
    return TG.api
      .get('/api/stats/overview', { silent: true })
      .then(render)
      .catch(function (error) {
        clear(refs.targets);
        refs.targets.appendChild(TG.tableMessageRow(TARGET_COLSPAN, '데이터를 불러오지 못했습니다.'));
        clear(refs.errorBox);
        refs.errorBox.appendChild(
          TG.errorState('통계를 불러오지 못했습니다.', error.message, function () {
            load();
          })
        );
      });
  }

  refs.refresh.addEventListener('click', function () {
    TG.guard(refs.refresh, load);
  });

  load();
})();
