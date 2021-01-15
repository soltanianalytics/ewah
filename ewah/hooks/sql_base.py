from ewah.hooks.base import EWWAHBaseHook


class EWAHSQLBaseHook(EWAHBaseHook):
    """Base hook extension for use as parent of various SQL hooks.

    Children need the following:
    - _get_db_conn method
    - _get_cursor property
    - _get_dictcursor property
    - execute method
    - get_data_from_sql method
    """

    _DEFAULT_PORT = 1234  # overwrite in child

    @staticmethod
    def get_connection_form_widgets() -> dict:
        """Returns connection widgets to add to connection form

        If you overwrite this in a child, make sure to include the fields defined herein
        """
        from flask_appbuilder.fieldwidgets import BS3TextFieldWidget
        from wtforms import StringField

        return {
            f"extra__{self.conn_type}__ssh_conn_id": StringField(
                "SSH Connection ID (optional)",
                widget=BS3TextFieldWidget(),
            ),
        }

    @property
    def dbconn(self):
        if not hasattr(self, "_dbconn"):
            if self.conn.ssh_conn_id:
                if not hasattr(self, "_ssh_hook"):
                    self._ssh_hook - EWAHBaseHook.get_hook_from_conn_id(
                        conn_id=self.conn.ssh_conn_id
                    )
                    self.local_bind_address = self._ssh_hook.start_tunnel(
                        self.conn.host, self.conn.port or self._DEFAULT_PORT
                    )
            else:
                self.local_bind_address = self.conn.host, self.conn.port
            self._dbconn = self._get_db_conn()
        return self._dbconn

    @property
    def cursor(self):
        """Cursor that returns lists of lists from the data source."""
        if not hasattr(self, "_cur"):
            self._cur = self._get_cursor()
        return self._cur

    @property
    def dictcursor(self):
        """Cursor that returns lists of dictionaries from the data source."""
        if not hasattr(self, "_dictcur"):
            self._dictcur = self._get_dictcursor()
        return self._dictcur

    def get_records(self, sql, parameters=None):
        """
        Variant of execute method. Required to work with the SQL sensor.
        """
        return self.execute_and_return_result(
            sql=sql, params=parameters, return_dict=False
        )

    def execute_and_return_result(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        return_dict: bool = False,
    ) -> Union[List[list], List[dict]]:
        cursor = self.dictcursor if return_dict else self.cursor
        self.execute(sql=sql, params=params, commit=False, cursor=cursor)
        return cursor.fetchall()

    def commit(self):
        self.log.info("Committing changes!")
        return self.dbconn.commit()

    def rollback(self):
        self.log.info("Rolling back changes!")
        return self.dbconn.rollback

    def close(self):
        if hasattr(self, "_cur"):
            self._cur.close()
            del self._cur
        if hasattr(self, "_dictcur"):
            self._dictcur.close()
            del self._dictcur
        if hasattr(self, "_dbconn"):
            if hasattr(self, "_ssh_hook"):
                self._ssh_hook.stop_tunnel()
                del self._ssh_hook
            self._dbcoon.close()
            del self._dbconn

    def __del__(self):
        self.close()
