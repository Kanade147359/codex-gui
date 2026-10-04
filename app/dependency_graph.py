"""Read-only projection of explicit task and scheduled-instruction dependencies. No model calls."""


def graph_view(db) -> dict:
    snapshot = db.dependency_graph_snapshot()
    tasks, errors, grouped = snapshot['tasks'], [], {}
    known = {t['id'] for t in tasks}

    def add(parent, child, setting):
        missing = [tid for tid in (parent, child) if tid not in known]
        if missing:
            errors.append(dict(message='参照先タスクが存在しません: ' + ', '.join(missing),
                               prerequisite=parent, successor=child, setting=setting))
            return  # Keep the bad reference in errors; never invent a node.
        if parent == child:
            errors.append(dict(message='自己参照の設定です: ' + parent,
                               prerequisite=parent, successor=child, setting=setting))
        grouped.setdefault((parent, child), []).append(setting)

    for edge in snapshot['initial']:
        add(edge['depends_on_task_id'], edge['task_id'], dict(kind='task', label='タスク開始の依存設定'))
    schedules = {s['id']: s for s in snapshot['schedules']}
    for edge in snapshot['scheduled_deps']:
        schedule = schedules.get(edge['scheduled_instruction_id'])
        if not schedule and edge['scheduled_instruction_id'] not in snapshot['schedule_ids']:
            errors.append(dict(message=f"依存設定の予約指示が存在しません: #{edge['scheduled_instruction_id']}"))
        if schedule:
            add(edge['depends_on_task_id'], schedule['task_id'],
                dict(kind='scheduled', id=schedule['id'], status=schedule['status'],
                     prompt=schedule['prompt'], blocked_reason=schedule['blocked_reason'],
                     label=f"予約指示 #{schedule['id']}"))
    edges = [dict(parent=p, child=c, key=f'{p}:{c}', settings=settings)
             for (p, c), settings in grouped.items()]
    # Projecting several turns onto task cards can contain cycles. Show them, without altering the scheduler.
    indegree = {tid: 0 for tid in known}
    children = {tid: [] for tid in known}
    for edge in edges:
        indegree[edge['child']] += 1
        children[edge['parent']].append(edge['child'])
    queue = sorted(tid for tid in known if indegree[tid] == 0)
    for tid in queue:
        for child in children[tid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(queue) != len(known):
        errors.append(dict(message='タスク単位の表示に循環があります。予約指示は別ターンのため、既存スケジュール画面で設定を確認してください。'))
    return dict(tasks=tasks, edges=edges, errors=errors)
