from hmm_core.api.routers import (
    admin,
    areas,
    attendance,
    catalog,
    feed,
    formation,
    identity,
    mentorship,
    notifications,
    onboarding,
    profiles,
    projects,
    recommendations,
    results,
    scores,
)

ROUTERS = [
    (domain.router, domain.internal)
    for domain in (
        identity,
        profiles,
        areas,
        onboarding,
        scores,
        catalog,
        formation,
        results,
        recommendations,
        notifications,
        admin,
        feed,
        attendance,
        projects,
        mentorship,
    )
]
