"""Daylight sentiment pipeline package.

Layering:
    domain/       -- pure logic, depends on nothing else in this tree
    infra/        -- external I/O (SharePoint, DB, LLM providers), depends only on domain/
    application/  -- orchestration, depends on domain/ + infra/ interfaces

The dependency direction is enforced by this folder structure; standalone
scripts in scripts/ import from domain/ and infra/ directly. See DESIGN.md
for the full architecture and current known issues.
"""
