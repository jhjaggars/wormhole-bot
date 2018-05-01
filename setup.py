from setuptools import setup, find_packages

if __name__ == "__main__":

    setup(
        name="wormhole",
        version="1.0.0",
        description="IRC bot that forwards messages to slack",
        packages=find_packages(),
        install_requires=[
            'tornado',
            'futures',
            'humanize',
            'python-dateutil'
        ]
    )
