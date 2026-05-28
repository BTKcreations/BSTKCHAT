from setuptools import setup, find_packages

setup(
    name="bstkchat",
    version="1.0.1",
    packages=find_packages(),
    install_requires=[
        "paho-mqtt>=1.6.1",
        "rich>=13.0.0",
        "prompt-toolkit>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "bstkchat=bstkchat.cli:main",
        ],
    },
    author="BSTK Developers",
    description="A real-time Terminal-based Chatting Application using unique room IDs.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Environment :: Console",
    ],
    python_requires=">=3.7",
)