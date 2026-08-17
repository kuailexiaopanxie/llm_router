"""Legacy setuptools entry point for editable installs with older pip."""

from setuptools import find_packages, setup


setup(
    name="coding-llm-router",
    version="0.2.0",
    description="Local-first deterministic router for Anthropic Messages and OpenAI Responses",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.12",
    package_dir={"": "src"},
    packages=find_packages("src"),
    install_requires=[
        "aiosqlite>=0.20,<1",
        "fastapi>=0.115,<1",
        "httpx>=0.27,<1",
        "prometheus-client>=0.21,<1",
        "pydantic>=2.9,<3",
        "PyYAML>=6.0,<7",
        "uvicorn[standard]>=0.32,<1",
    ],
    extras_require={
        "dev": [
            "mypy>=1.13,<2",
            "pytest>=8.3,<9",
            "ruff>=0.8,<1",
        ]
    },
    entry_points={"console_scripts": ["llm-router=llm_router.app:main"]},
)
