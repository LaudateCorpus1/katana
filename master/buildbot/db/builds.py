# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members
from datetime import datetime, timedelta
from twisted.internet import defer

from twisted.internet import reactor
from buildbot.db import base
from buildbot.util import epoch2datetime, datetime2epoch
import sqlalchemy as sa
from buildbot.db.buildrequests import maybeFilterBuildRequestsBySourceStamps, mkdt


class BuildDoNotExist(Exception):
    pass


class BuildsConnectorComponent(base.DBConnectorComponent):
    # Documentation is in developer/database.rst

    def getBuild(self, bid):
        def thd(conn):
            tbl = self.db.model.builds
            res = conn.execute(tbl.select(whereclause=(tbl.c.id == bid)))
            row = res.fetchone()

            rv = None
            if row:
                rv = self._bdictFromRow(row)
            res.close()
            return rv
        return self.db.pool.do(thd)

    def getBuildsAndResultForRequest(self, brid):
        def thd(conn):
            builds_tbl = self.db.model.builds
            buildrequest_tbl = self.db.model.buildrequests
            q = sa.select([builds_tbl.c.id, builds_tbl.c.number, buildrequest_tbl.c.id.label("brid"), builds_tbl.c.start_time,
                                   builds_tbl.c.finish_time, buildrequest_tbl.c.results],
                                  from_obj= buildrequest_tbl.outerjoin(builds_tbl,
                                                        (buildrequest_tbl.c.id == builds_tbl.c.brid)),
                                  whereclause=(buildrequest_tbl.c.id == brid))
            res = conn.execute(q)
            return [ self._bdictFromRow(row)
                     for row in res.fetchall() ]
        return self.db.pool.do(thd)

    def getBuildsForRequest(self, brid):
        def thd(conn):
            tbl = self.db.model.builds
            q = tbl.select(whereclause=(tbl.c.brid == brid))
            res = conn.execute(q)
            return [ self._bdictFromRow(row) for row in res.fetchall() ]
        return self.db.pool.do(thd)

    def getBuildNumberForRequest(self, brid):
        def thd(conn):
            tbl = self.db.model.builds
            q = sa.select(columns=[sa.func.max(tbl.c.number).label("number")]).where(tbl.c.brid == brid)
            res = conn.execute(q)
            row = res.fetchone()
            if row:
                return row.number
            return None
        return self.db.pool.do(thd)

    def getBuildIDForRequest(self, brid, build_number):
        def thd(conn):
            tbl = self.db.model.builds
            q = sa.select([tbl.c.id]) \
                .where(tbl.c.brid == brid) \
                .where(tbl.c.number == build_number)
            res = conn.execute(q)
            row = res.fetchone()

            if not row:
                msg = "There is no build for brid: {brid} and build number {build_number}".format(
                    brid=brid,
                    build_number=build_number,
                )
                raise BuildDoNotExist(msg)

            return row.id

        return self.db.pool.do(thd)

    def getBuildNumbersForRequests(self, brids):
        def thd(conn):
            tbl = self.db.model.builds
            q = sa.select(columns=[sa.func.max(tbl.c.number).label("number"), tbl.c.brid])\
                .where(tbl.c.brid.in_(brids))\
                .group_by(tbl.c.number, tbl.c.brid)
            res = conn.execute(q)
            rows = res.fetchall()
            rv = []
            if rows:
                for row in rows:
                    if row.number not in rv:
                        rv.append(row.number)
            res.close()
            return rv
        return self.db.pool.do(thd)

    def addBuild(self, brid, number, slavename=None, _reactor=reactor):
        def thd(conn):
            start_time = _reactor.seconds()
            r = conn.execute(self.db.model.builds.insert(),
                    dict(number=number, brid=brid, slavename=slavename, start_time=start_time,
                        finish_time=None))
            return r.inserted_primary_key[0]
        return self.db.pool.do(thd)

    def finishBuilds(self, bids, _reactor=reactor):
        def thd(conn):
            transaction = conn.begin()
            tbl = self.db.model.builds
            now = _reactor.seconds()

            # split the bids into batches, so as not to overflow the parameter
            # lists of the database interface
            remaining = bids
            while remaining:
                batch, remaining = remaining[:100], remaining[100:]
                q = tbl.update(whereclause=(tbl.c.id.in_(batch)))
                conn.execute(q, finish_time=now)

            transaction.commit()
        return self.db.pool.do(thd)

    def createBuildUser(self, buildid, userid, finish_time):
        def thd(conn):
            tbl = self.db.model.build_user

            q = tbl.insert()
            conn.execute(q, dict(buildid=buildid, userid=userid, finish_time=finish_time))

        return self.db.pool.do(thd)

    def finishedMergedBuilds(self, brids, number):
        def thd(conn):
            if len(brids) > 1:
                builds_tbl = self.db.model.builds

                q = sa.select([builds_tbl.c.number, builds_tbl.c.finish_time])\
                    .where(builds_tbl.c.brid == brids[0])\
                    .where(builds_tbl.c.number == number)

                res = conn.execute(q)
                row = res.fetchone()
                if row:
                    stmt = builds_tbl.update()\
                        .where(builds_tbl.c.brid.in_(brids))\
                        .where(builds_tbl.c.number==number)\
                        .where(builds_tbl.c.finish_time == None)\
                        .values(finish_time = row.finish_time)

                    res = conn.execute(stmt)
                    return res.rowcount

        return self.db.pool.do(thd)

    def getLastsBuildsNumbersBySlave(self, slavename, results=None, num_builds=15):
        def thd(conn):
            buildrequests_tbl = self.db.model.buildrequests
            builds_tbl = self.db.model.builds

            lastBuilds = {}
            maxSearch = num_builds if num_builds < 200 else 200
            resumeBuilds = [9, -1]

            q = sa.select(columns=[buildrequests_tbl.c.id, buildrequests_tbl.c.buildername, builds_tbl.c.number],
                          from_obj=buildrequests_tbl.join(builds_tbl,
                                                          (buildrequests_tbl.c.id == builds_tbl.c.brid)
                                                          & (builds_tbl.c.finish_time != None)))\
                .group_by(buildrequests_tbl.c.id, buildrequests_tbl.c.buildername, builds_tbl.c.number)

            #TODO: support filter by RETRY result
            if results:
                q = sa.select(columns=[buildrequests_tbl.c.id,
                                       buildrequests_tbl.c.buildername,
                                       buildrequests_tbl.c.results,
                                       sa.func.max(builds_tbl.c.number).label("number")],
                          from_obj=buildrequests_tbl.join(builds_tbl,
                                                          (buildrequests_tbl.c.id == builds_tbl.c.brid)
                                                          & (builds_tbl.c.finish_time != None)))\
                    .where(buildrequests_tbl.c.results.in_(results))\
                    .group_by(buildrequests_tbl.c.id, buildrequests_tbl.c.buildername,
                              buildrequests_tbl.c.results)

            q = q.where(buildrequests_tbl.c.mergebrid == None)\
                .where(buildrequests_tbl.c.complete == 1)\
                .where(~buildrequests_tbl.c.results.in_(resumeBuilds))\
                .where(builds_tbl.c.slavename == slavename)\
                .order_by(sa.desc(buildrequests_tbl.c.complete_at)).limit(maxSearch)

            res = conn.execute(q)

            rows = res.fetchall()
            if rows:
                for row in rows:
                    if row.buildername not in lastBuilds:
                        lastBuilds[row.buildername] = [row.number]
                    else:
                        lastBuilds[row.buildername].append(row.number)

            res.close()

            return lastBuilds

        return self.db.pool.do(thd)

    def getLastBuildsNumbers(self, buildername=None, sourcestamps=None, results=None, num_builds=15):
        def thd(conn):
            buildrequests_tbl = self.db.model.buildrequests
            buildsets_tbl = self.db .model.buildsets
            sourcestampsets_tbl = self.db.model.sourcestampsets
            sourcestamps_tbl = self.db.model.sourcestamps
            builds_tbl = self.db.model.builds

            lastBuilds = []
            maxSearch = num_builds if num_builds < 200 else 200
            resumeBuilds = [9, -1]

            q = sa.select(columns=[buildrequests_tbl.c.id, sa.func.max(builds_tbl.c.number).label("number")],
                          from_obj=buildrequests_tbl.join(builds_tbl,
                                                          (buildrequests_tbl.c.id == builds_tbl.c.brid)
                                                          & (builds_tbl.c.finish_time != None))).\
                where(buildrequests_tbl.c.mergebrid == None)\
                .where(~buildrequests_tbl.c.results.in_(resumeBuilds))\
                .where(buildrequests_tbl.c.buildername == buildername)\
                .where(buildrequests_tbl.c.complete == 1)\
                .group_by(buildrequests_tbl.c.id)

            #TODO: support filter by RETRY result
            if results:
                q = sa.select(columns=[buildrequests_tbl.c.id, buildrequests_tbl.c.results,
                                       sa.func.max(builds_tbl.c.number).label("number")],
                          from_obj=buildrequests_tbl.join(builds_tbl,
                                                          (buildrequests_tbl.c.id == builds_tbl.c.brid)
                                                          & (builds_tbl.c.finish_time != None))).\
                    where(buildrequests_tbl.c.mergebrid == None)\
                    .where(buildrequests_tbl.c.buildername == buildername)\
                    .where(buildrequests_tbl.c.results.in_(results))\
                    .where(buildrequests_tbl.c.complete == 1)\
                    .group_by(buildrequests_tbl.c.id, buildrequests_tbl.c.results)

            q = maybeFilterBuildRequestsBySourceStamps(query=q,
                                                       sourcestamps=sourcestamps,
                                                       buildrequests_tbl=buildrequests_tbl,
                                                       buildsets_tbl=buildsets_tbl,
                                                       sourcestamps_tbl=sourcestamps_tbl,
                                                       sourcestampsets_tbl=sourcestampsets_tbl)

            q = q.order_by(sa.desc(buildrequests_tbl.c.complete_at)).limit(maxSearch)

            res = conn.execute(q)

            rows = res.fetchall()
            if rows:
                for row in rows:
                    if row.number not in lastBuilds:
                        lastBuilds.append(row.number)

            res.close()

            return lastBuilds

        return self.db.pool.do(thd)

    def getLastBuildsOwnedBy(self, user_id, botmaster, day_count):
        def thd(conn):
            buildrequests_tbl = self.db.model.buildrequests
            buildsets_tbl = self.db.model.buildsets
            builds_tbl = self.db.model.builds
            builduser_tbl = self.db.model.build_user

            from_time = datetime2epoch(datetime.now() - timedelta(days=day_count))

            from_clause = buildsets_tbl.join(
                buildrequests_tbl,
                buildrequests_tbl.c.buildsetid == buildsets_tbl.c.id
            ).join(
                builds_tbl,
                builds_tbl.c.brid == buildrequests_tbl.c.id
            ).join(
                builduser_tbl,
                builduser_tbl.c.buildid == builds_tbl.c.id
            )

            q = (
                sa.select([buildrequests_tbl, builds_tbl, buildsets_tbl], use_labels=True)
                .select_from(from_clause)
                .where(builds_tbl.c.finish_time >= from_time)
                .where(builduser_tbl.c.userid == user_id)
                .order_by(sa.desc(builds_tbl.c.start_time))
            )

            res = conn.execute(q)

            last_builds = []
            for row in res.fetchall():
                buildername = row.buildrequests_buildername
                last_builds.append(dict(
                    buildername=buildername,
                    friendly_name=botmaster.master.status.getFriendlyName(buildername) or buildername,
                    complete=bool(row.buildrequests_complete),
                    builds_id=row.builds_id,
                    builds_number=row.builds_number,
                    reason=row.buildsets_reason,
                    project=botmaster.getBuilderConfig(row.buildrequests_buildername).project,
                    slavename=row.builds_slavename,
                    submitted_at=mkdt(row.buildrequests_submitted_at),
                    complete_at=mkdt(row.buildrequests_complete_at),
                    sourcestampsetid=row.buildsets_sourcestampsetid,
                    results=row.buildrequests_results,
                ))
            return last_builds


        return self.db.pool.do(thd)

    def createFullBuildObject(self, branch, revision, repository, project, reason, submitted_at,
                              complete_at, buildername, slavepool, number, slavename, results, codebase):
        """ This method creates a new build object with all required associated objects

        :param branch: a string value with branch name (on this branch code was built)
        :param revision: a string value with revision (on this revision code was built)
        :param repository: a string value with path to repository
        :param project: a string value with project name
        :param reason: a string value described why build was executed
        :param submitted_at: an integer value described when build was executed
        :param complete_at: an integer value describe when build was completed or None when is still in progress
        :param buildername: an string value with builder name, this name must exists in master.cfg
        :param slavepool: a string value with slave pool name
        :param number: an integer value with build number. Must be unique with build request id
        :param slavename: a string value with slave name
        :param results: an integer value with results status. See available options: master.buildbot.status.results
        :param codebase: a string value with codebase of repository
        :return: defer value
        """
        def thd(conn):
            transaction = conn.begin()
            try:
                # Create sourcestampsets
                r = conn.execute(self.db.model.sourcestampsets.insert(), dict())
                sourcestampsset_id = r.inserted_primary_key[0]

                # Create sourcestamps
                conn.execute(self.db.model.sourcestamps.insert(), {
                    'branch': branch,
                    'revision': revision,
                    'patchid': None,
                    'repository': repository,
                    'codebase': codebase,
                    'project': project,
                    'sourcestampsetid': sourcestampsset_id,
                })

                # Create buildsets
                res = conn.execute(self.db.model.buildsets.insert(), {
                    'reason': reason,
                    'sourcestampsetid': sourcestampsset_id,
                    'submitted_at': submitted_at,
                    'complete': bool(complete_at),
                    'complete_at': complete_at,
                    'results': results,
                })
                buildset_id = res.inserted_primary_key[0]

                # Create buildrequests
                res = conn.execute(self.db.model.buildrequests.insert(), {
                    'buildsetid': buildset_id,
                    'buildername': buildername,
                    'proiority': 50,
                    'complete': bool(complete_at),
                    'results': results,
                    'submitted_at': submitted_at,
                    'complete_at': complete_at,
                    'slavepool': slavepool,
                })
                buildrequest_id = res.inserted_primary_key[0]

                # Create builds
                conn.execute(self.db.model.builds.insert(), {
                    'number': number,
                    'brid': buildrequest_id,
                    'slavename': slavename,
                    'start_time': submitted_at,
                    'finish_time': complete_at,
                })
                transaction.commit()
            except Exception as e:
                print("Exception occurs during create new build", e)
                transaction.rollback()
                raise

        return self.db.pool.do(thd)

    def _bdictFromRow(self, row):
        def mkdt(epoch):
            if epoch:
                return epoch2datetime(epoch)

        _bdict = dict(
            bid=row.id,
            brid=row.brid,
            number=row.number,
            start_time=mkdt(row.start_time),
            finish_time=mkdt(row.finish_time))
        if 'results' in row.keys():
            _bdict['results'] = row.results
        return _bdict
