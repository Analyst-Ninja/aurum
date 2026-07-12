"""
Base API Data Source
"""

from abc import ABC, abstractmethod
from dotenv import load_dotenv
import os

from sqlalchemy import create_engine

load_dotenv()  # Load environment variables from .env file
POSTGRES_URL = os.getenv("POSTGRES_URL")


class BaseAPIDataSource(ABC):
    """
    A base class for API data sources.
    """

    def __init__(self):
        pass  # Initialize any required attributes here

    @abstractmethod
    def read_data(self, data, *args, **kwargs):
        """
        Retrieve data from the API.

        :return: Data retrieved from the API.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def write_data(self, data, *args, **kwargs):
        """
        Write data to the API or a specified destination.

        :param data: Data to be written.
        :return: Result of the write operation.
        """

        if not POSTGRES_URL:
            raise ValueError("POSTGRES_URL is not set in the environment variables.")

        engine = create_engine(url=POSTGRES_URL, echo=False)

        # 4. Write the DataFrame to the PostgreSQL table
        data.to_sql(
            name="ohlcv_1d",  # Name of the target SQL table
            con=engine,  # SQLAlchemy engine connection
            if_exists="append",  # How to behave if the table already exists
            index=False,  # Do not write the DataFrame index as a separate column
        )
        print("Data successfully loaded!")
