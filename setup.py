from setuptools import setup, find_packages

setup(
    name="soundcloud-to-youtube",
    version="1.2.0",
    packages=find_packages(),
    install_requires=[
        "google-api-python-client",
        "google-auth-oauthlib",
        "mutagen",
        "requests",
        "scdl",
    ],
    entry_points={
        "console_scripts": [
            "soundcloud-to-youtube = soundcloud_to_youtube.cli:main",
        ],
    },
    author="Psylooo",
    description="SoundCloud to YouTube uploader with best quality",
    python_requires=">=3.8",
)
