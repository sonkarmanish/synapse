# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import attr

from synapse.api.constants import RelationTypes
from synapse.api.errors import SynapseError
from synapse.storage._base import SQLBaseStore
from synapse.storage.stream import generate_pagination_where_clause
from synapse.util.caches.descriptors import cached

logger = logging.getLogger(__name__)


@attr.s
class PaginationChunk(object):
    """Returned by relation pagination APIs.

    Attributes:
        chunk (list): The rows returned by pagination
        next_batch (Any|None): Token to fetch next set of results with, if
            None then there are no more results.
        prev_batch (Any|None): Token to fetch previous set of results with, if
            None then there are no previous results.
    """

    chunk = attr.ib()
    next_batch = attr.ib(default=None)
    prev_batch = attr.ib(default=None)

    def to_dict(self):
        d = {"chunk": self.chunk}

        if self.next_batch:
            d["next_batch"] = self.next_batch.to_string()

        if self.prev_batch:
            d["prev_batch"] = self.prev_batch.to_string()

        return d


@attr.s
class RelationPaginationToken(object):
    """Pagination token for relation pagination API.

    As the results are order by topological ordering, we can use the
    `topological_ordering` and `stream_ordering` fields of the events at the
    boundaries of the chunk as pagination tokens.

    Attributes:
        topological (int): The topological ordering of the boundary event
        stream (int): The stream ordering of the boundary event.
    """

    topological = attr.ib()
    stream = attr.ib()

    @staticmethod
    def from_string(string):
        try:
            t, s = string.split("-")
            return RelationPaginationToken(int(t), int(s))
        except ValueError:
            raise SynapseError(400, "Invalid token")

    def to_string(self):
        return "%d-%d" % (self.topological, self.stream)


@attr.s
class AggregationPaginationToken(object):
    """Pagination token for relation aggregation pagination API.

    As the results are order by count and then MAX(stream_ordering) of the
    aggregation groups, we can just use them as our pagination token.

    Attributes:
        count (int): The count of relations in the boundar group.
        stream (int): The MAX stream ordering in the boundary group.
    """

    count = attr.ib()
    stream = attr.ib()

    @staticmethod
    def from_string(string):
        try:
            c, s = string.split("-")
            return AggregationPaginationToken(int(c), int(s))
        except ValueError:
            raise SynapseError(400, "Invalid token")

    def to_string(self):
        return "%d-%d" % (self.count, self.stream)


class RelationsWorkerStore(SQLBaseStore):
    @cached(tree=True)
    def get_relations_for_event(
        self,
        event_id,
        relation_type=None,
        event_type=None,
        aggregation_key=None,
        limit=5,
        direction="b",
        from_token=None,
        to_token=None,
    ):
        """Get a list of relations for an event, ordered by topological ordering.

        Args:
            event_id (str): Fetch events that relate to this event ID.
            relation_type (str|None): Only fetch events with this relation
                type, if given.
            event_type (str|None): Only fetch events with this event type, if
                given.
            aggregation_key (str|None): Only fetch events with this aggregation
                key, if given.
            limit (int): Only fetch the most recent `limit` events.
            direction (str): Whether to fetch the most recent first (`"b"`) or
                the oldest first (`"f"`).
            from_token (RelationPaginationToken|None): Fetch rows from the given
                token, or from the start if None.
            to_token (RelationPaginationToken|None): Fetch rows up to the given
                token, or up to the end if None.

        Returns:
            Deferred[PaginationChunk]: List of event IDs that match relations
            requested. The rows are of the form `{"event_id": "..."}`.
        """

        if from_token:
            from_token = RelationPaginationToken.from_string(from_token)

        if to_token:
            to_token = RelationPaginationToken.from_string(to_token)

        where_clause = ["relates_to_id = ?"]
        where_args = [event_id]

        if relation_type is not None:
            where_clause.append("relation_type = ?")
            where_args.append(relation_type)

        if event_type is not None:
            where_clause.append("type = ?")
            where_args.append(event_type)

        if aggregation_key:
            where_clause.append("aggregation_key = ?")
            where_args.append(aggregation_key)

        pagination_clause = generate_pagination_where_clause(
            direction=direction,
            column_names=("topological_ordering", "stream_ordering"),
            from_token=attr.astuple(from_token) if from_token else None,
            to_token=attr.astuple(to_token) if to_token else None,
            engine=self.database_engine,
        )

        if pagination_clause:
            where_clause.append(pagination_clause)

        if direction == "b":
            order = "DESC"
        else:
            order = "ASC"

        sql = """
            SELECT event_id, topological_ordering, stream_ordering
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE %s
            ORDER BY topological_ordering %s, stream_ordering %s
            LIMIT ?
        """ % (
            " AND ".join(where_clause),
            order,
            order,
        )

        def _get_recent_references_for_event_txn(txn):
            txn.execute(sql, where_args + [limit + 1])

            last_topo_id = None
            last_stream_id = None
            events = []
            for row in txn:
                events.append({"event_id": row[0]})
                last_topo_id = row[1]
                last_stream_id = row[2]

            next_batch = None
            if len(events) > limit and last_topo_id and last_stream_id:
                next_batch = RelationPaginationToken(last_topo_id, last_stream_id)

            return PaginationChunk(
                chunk=list(events[:limit]), next_batch=next_batch, prev_batch=from_token
            )

        return self.runInteraction(
            "get_recent_references_for_event", _get_recent_references_for_event_txn
        )

    @cached(tree=True)
    def get_aggregation_groups_for_event(
        self,
        event_id,
        event_type=None,
        limit=5,
        direction="b",
        from_token=None,
        to_token=None,
    ):
        """Get a list of annotations on the event, grouped by event type and
        aggregation key, sorted by count.

        This is used e.g. to get the what and how many reactions have happend
        on an event.

        Args:
            event_id (str): Fetch events that relate to this event ID.
            event_type (str|None): Only fetch events with this event type, if
                given.
            limit (int): Only fetch the `limit` groups.
            direction (str): Whether to fetch the highest count first (`"b"`) or
                the lowest count first (`"f"`).
            from_token (AggregationPaginationToken|None): Fetch rows from the
                given token, or from the start if None.
            to_token (AggregationPaginationToken|None): Fetch rows up to the
                given token, or up to the end if None.


        Returns:
            Deferred[PaginationChunk]: List of groups of annotations that
            match. Each row is a dict with `type`, `key` and `count` fields.
        """

        if from_token:
            from_token = AggregationPaginationToken.from_string(from_token)

        if to_token:
            to_token = AggregationPaginationToken.from_string(to_token)

        where_clause = ["relates_to_id = ?", "relation_type = ?"]
        where_args = [event_id, RelationTypes.ANNOTATION]

        if event_type:
            where_clause.append("type = ?")
            where_args.append(event_type)

        having_clause = generate_pagination_where_clause(
            direction=direction,
            column_names=("COUNT(*)", "MAX(stream_ordering)"),
            from_token=attr.astuple(from_token) if from_token else None,
            to_token=attr.astuple(to_token) if to_token else None,
            engine=self.database_engine,
        )

        if direction == "b":
            order = "DESC"
        else:
            order = "ASC"

        if having_clause:
            having_clause = "HAVING " + having_clause
        else:
            having_clause = ""

        sql = """
            SELECT type, aggregation_key, COUNT(*), MAX(stream_ordering)
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE {where_clause}
            GROUP BY relation_type, type, aggregation_key
            {having_clause}
            ORDER BY COUNT(*) {order}, MAX(stream_ordering) {order}
            LIMIT ?
        """.format(
            where_clause=" AND ".join(where_clause),
            order=order,
            having_clause=having_clause,
        )

        def _get_aggregation_groups_for_event_txn(txn):
            txn.execute(sql, where_args + [limit + 1])

            next_batch = None
            events = []
            for row in txn:
                events.append({"type": row[0], "key": row[1], "count": row[2]})
                next_batch = AggregationPaginationToken(row[2], row[3])

            if len(events) <= limit:
                next_batch = None

            return PaginationChunk(
                chunk=list(events[:limit]), next_batch=next_batch, prev_batch=from_token
            )

        return self.runInteraction(
            "get_aggregation_groups_for_event", _get_aggregation_groups_for_event_txn
        )


class RelationsStore(RelationsWorkerStore):
    def _handle_event_relations(self, txn, event):
        """Handles inserting relation data during peristence of events

        Args:
            txn
            event (EventBase)
        """
        relation = event.content.get("m.relates_to")
        if not relation:
            # No relations
            return

        rel_type = relation.get("rel_type")
        if rel_type not in (
            RelationTypes.ANNOTATION,
            RelationTypes.REFERENCES,
            RelationTypes.REPLACES,
        ):
            # Unknown relation type
            return

        parent_id = relation.get("event_id")
        if not parent_id:
            # Invalid relation
            return

        aggregation_key = relation.get("key")

        self._simple_insert_txn(
            txn,
            table="event_relations",
            values={
                "event_id": event.event_id,
                "relates_to_id": parent_id,
                "relation_type": rel_type,
                "aggregation_key": aggregation_key,
            },
        )

        txn.call_after(self.get_relations_for_event.invalidate_many, (parent_id,))
        txn.call_after(
            self.get_aggregation_groups_for_event.invalidate_many, (parent_id,)
        )
