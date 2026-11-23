from collaboration_runtime.scheduler import _delivery_guidance


def test_guidance_is_limited_to_actionable_task_events():
    def guidance(purpose):
        return _delivery_guidance({'delivery': {'purpose': purpose}})

    review = guidance('task.submitted')
    assert set(review) == {'review'}
    assert 'evidence' in review['review'] and 'submitted versions' in review['review']
    for purpose in ('task.assigned', 'task.ready', 'task.returned'):
        delivery = guidance(purpose)
        assert set(delivery) == {'delivery'} and 'compact=true' in delivery['delivery']
    assert guidance('clarification.resume') is None
    assert guidance('message') is None
