import logging
import time
from functools import partial
from multiprocessing import Pool
from string import Template

import psycopg2
import sqlalchemy as sql

index_template = Template('CREATE INDEX $idx_title ON "$table" ($attrs)')
drop_table_template = Template('DROP TABLE IF EXISTS "$table"')
create_table_template = Template('CREATE TABLE "$table" AS ($stmt)')
create_gist_index_template = Template('CREATE INDEX $idx_title ON "$table" USING GIST ($spatial_attr)')
cluster_table_on_index_template = Template('CLUSTER "$table" USING $idx_title')


class DBengine:
    """
    A wrapper class for postgresql engine.
    Maintains connections and executes queries.
    """

    def __init__(self, user, pwd, db, host='localhost', port=5432, pool_size=20, timeout=60000):
        self.timeout = timeout
        self._pool = Pool(pool_size) if pool_size > 1 else None
        url = 'postgresql+psycopg2://{}:{}@{}:{}/{}?client_encoding=utf8'
        url = url.format(user, pwd, host, port, db)
        self.conn = url
        con = 'dbname={} user={} password={} host={} port={}'
        con = con.format(db, user, pwd, host, port)
        self.conn_args = con
        self.engine = sql.create_engine(url, client_encoding='utf8', pool_size=pool_size)
        self.polars_conn = f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

    def execute_queries(self, queries):
        """
        Executes :param queries: in parallel.

        :param queries: (list[str]) list of SQL queries to be executed
        """
        logging.debug('Preparing to execute %d queries.', len(queries))
        tic = time.perf_counter()
        results = self._apply_func(partial(_execute_query, conn_args=self.conn_args), [(idx, q) for idx, q in enumerate(queries)])
        toc = time.perf_counter()
        logging.debug('Time to execute %d queries: %.2f secs', len(queries), toc - tic)
        return results

    def execute_queries_w_backup(self, queries):
        """
        Executes :param queries: that have backups in parallel. Used in featurization.

        :param queries: (list[str]) list of SQL queries to be executed
        """
        logging.debug('Preparing to execute %d queries.', len(queries))
        tic = time.perf_counter()
        results = self._apply_func(
            partial(_execute_query_w_backup, conn_args=self.conn_args, timeout=self.timeout),
            [(idx, q) for idx, q in enumerate(queries)])
        toc = time.perf_counter()
        logging.debug('Time to execute %d queries: %.2f secs', len(queries), toc - tic)
        return results

    def execute_query(self, query):
        """
        Executes a single :param query: using current connection.

        :param query: (str) SQL query to be executed
        """
        tic = time.perf_counter()
        conn = self.engine.connect()
        query = sql.text(query)
        result = conn.execute(query).fetchall()
        conn.close()
        toc = time.perf_counter()
        logging.debug('Time to execute query: %.2f secs', toc - tic)
        return result

    def create_db_table_from_query(self, name, query):
        tic = time.perf_counter()
        drop = drop_table_template.substitute(table=name)
        create = create_table_template.substitute(table=name, stmt=query)
        with self.engine.begin() as conn:
            drop = sql.text(drop)
            create = sql.text(create)
            conn.execute(drop)
            conn.execute(create)
        toc = time.perf_counter()
        logging.debug('Time to create table: %.2f secs', toc - tic)
        return True

    def create_db_index(self, name, table, attr_list):
        """
        create_db_index creates a (multi-column) index on the columns/attributes
        specified in :param attr_list: with the given :param name: on
        :param table:.

        :param name: (str) name of index
        :param table: (str) name of table
        :param attr_list: (list[str]) list of attributes/columns to create index on
        """
        # We need to quote each attribute since Postgres auto-downcases unquoted column references
        quoted_attrs = map(lambda attr: '"{}"'.format(attr), attr_list)
        stmt = index_template.substitute(idx_title=name, table=table, attrs=','.join(quoted_attrs))
        tic = time.perf_counter()
        with self.engine.begin() as conn:
            stmt = sql.text(stmt)
            result = conn.execute(stmt)
        toc = time.perf_counter()
        logging.debug('Time to create index: %.2f secs', toc - tic)
        return result

    def create_spatial_db_index(self, name, table, spatial_attr):
        """
        create_spatial_db_index creates a GIST index on the column
        specified in :param spatial_attr: with the given :param name: on
        :param table:.
        :param name: (str) name of index
        :param table: (str) name of table
        :param spatial_attr: spatial attribute to create index on
        """
        # We need to quote each attribute since Postgres auto-downcases unquoted column references
        s_attr = f'"{spatial_attr}"'
        sql_create_gist = create_gist_index_template.substitute(idx_title=name, table=table, spatial_attr=s_attr)
        tic = time.perf_counter()
        with self.engine.begin() as conn:
            sql_create_gist = sql.text(sql_create_gist)
            result = conn.execute(sql_create_gist)
        toc = time.perf_counter()
        logging.debug('SPARCLE: Time to create spatial index: %.2f secs', toc - tic)
        return result

    def cluster_db_using_index(self, table_name, index_name):
        sql_cluster = cluster_table_on_index_template.substitute(idx_title=index_name, table=table_name)
        tic = time.perf_counter()
        with self.engine.begin() as conn:
            sql_cluster = sql.text(sql_cluster)
            result = conn.execute(sql_cluster)
        toc = time.perf_counter()
        logging.debug('SPARCLE: Time to cluster table on index: %.2f secs', toc - tic)
        return result

    def _apply_func(self, func, collection):
        if self._pool is None:
            return list(map(func, collection))
        return self._pool.map(func, collection)


def _execute_query(args, conn_args):
    query_id = args[0]
    query = args[1]
    logging.debug("Starting to execute query %s with id %s", query, query_id)
    tic = time.perf_counter()
    con = psycopg2.connect(conn_args)
    cur = con.cursor()
    cur.execute(query)
    res = cur.fetchall()
    con.close()
    toc = time.perf_counter()
    logging.debug('Time to execute query with id %d: %.2f secs', query_id, (toc - tic))
    return res


def _execute_query_w_backup(args, conn_args, timeout):
    query_id = args[0]
    query = args[1][0]
    query_backup = args[1][1]
    logging.debug("Starting to execute query %s with id %s", query, query_id)
    tic = time.perf_counter()
    con = psycopg2.connect(conn_args)
    cur = con.cursor()
    cur.execute("SET statement_timeout to %d;" % timeout)
    try:
        cur.execute(query)
        res = cur.fetchall()
    except psycopg2.extensions.QueryCanceledError as e:
        logging.debug("Failed to execute query %s with id %s. Timeout reached.", query, query_id)

        # No backup query, simply return empty result
        if not query_backup:
            logging.warn("no backup query to execute, returning empty query results")
            return []

        logging.debug("Starting to execute backup query %s with id %s", query_backup, query_id)
        con.close()
        con = psycopg2.connect(conn_args)
        cur = con.cursor()
        cur.execute(query_backup)
        res = cur.fetchall()
        con.close()
    toc = time.perf_counter()
    logging.debug('Time to execute query with id %d: %.2f secs', query_id, toc - tic)
    return res
