# -*- coding: utf-8 -*-
# Copyright (c) 2017  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Jan Kaluza <jkaluza@redhat.com>

""" SQLAlchemy Database models for the Flask app
"""

import flask
import json

from datetime import datetime
from sqlalchemy.orm import (validates, relationship)

from flask_login import UserMixin

from freshmaker import app, db, log
from freshmaker.types import ArtifactType, ArtifactBuildState
from freshmaker.events import (
    MBSModuleStateChangeEvent, GitModuleMetadataChangeEvent,
    GitRPMSpecChangeEvent, TestingEvent, GitDockerfileChangeEvent,
    BodhiUpdateCompleteStableEvent, KojiTaskStateChangeEvent, BrewSignRPMEvent,
    ErrataAdvisoryRPMsSignedEvent)

EVENT_TYPES = {
    MBSModuleStateChangeEvent: 0,
    GitModuleMetadataChangeEvent: 1,
    GitRPMSpecChangeEvent: 2,
    TestingEvent: 3,
    GitDockerfileChangeEvent: 4,
    BodhiUpdateCompleteStableEvent: 5,
    KojiTaskStateChangeEvent: 6,
    BrewSignRPMEvent: 7,
    ErrataAdvisoryRPMsSignedEvent: 8,
}

INVERSE_EVENT_TYPES = {v: k for k, v in EVENT_TYPES.items()}


def commit_on_success(func):
    """
    Ensures db session is committed after a successful call to decorated
    function, otherwise rollback.
    """
    def _decorator(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            db.session.rollback()
            raise
        finally:
            db.session.commit()
    return _decorator


class FreshmakerBase(db.Model):
    __abstract__ = True


class User(FreshmakerBase, UserMixin):
    """User information table"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(200), nullable=False, unique=True)

    @classmethod
    def find_user_by_name(cls, username):
        """Find a user by username

        :param str username: a string of username to find user
        :return: user object if found, otherwise None is returned.
        :rtype: User
        """
        try:
            return db.session.query(cls).filter(cls.username == username)[0]
        except IndexError:
            return None

    @classmethod
    def create_user(cls, username):
        user = cls(username=username)
        db.session.add(user)
        return user


class Event(FreshmakerBase):
    __tablename__ = "events"
    id = db.Column(db.Integer, primary_key=True)
    # ID of message generating the rebuild event.
    message_id = db.Column(db.String, nullable=False)
    # Searchable key for the event - used when searching for events from the JSON
    # API.
    search_key = db.Column(db.String, nullable=False)
    # Event type id defined in EVENT_TYPES - ID of class inherited from
    # BaseEvent class - used when searching for events of particular type.
    event_type_id = db.Column(db.Integer, nullable=False)
    # True when the Event is already released and we do not have to include
    # it in the future rebuilds of artifacts.
    # This is currently only used for internal Docker images rebuilds, but in
    # the future might be used even for modules or Fedora Docker images.
    released = db.Column(db.Boolean, default=True)

    # List of builds associated with this Event.
    builds = relationship("ArtifactBuild", back_populates="event")

    compose_id = db.Column(
        db.Integer,
        default=None,
        doc='Used to include new version packages to rebuild docker images')

    @classmethod
    def create(cls, session, message_id, search_key, event_type, released=True):
        if event_type in EVENT_TYPES:
            event_type = EVENT_TYPES[event_type]
        event = cls(
            message_id=message_id,
            search_key=search_key,
            event_type_id=event_type,
            released=released,
        )
        session.add(event)
        return event

    @classmethod
    def get(cls, session, message_id):
        return session.query(cls).filter_by(message_id=message_id).first()

    @classmethod
    def get_or_create(cls, session, message_id, search_key, event_type, released=True):
        instance = cls.get(session, message_id)
        if instance:
            return instance
        return cls.create(session, message_id, search_key, event_type, released)

    @classmethod
    def get_unreleased(cls, session):
        return session.query(cls).filter_by(released=False).all()

    @property
    def event_type(self):
        return INVERSE_EVENT_TYPES[self.event_type_id]

    def add_event_dependency(self, session, event):
        dep = EventDependency(event_id=self.id,
                              event_dependency_id=event.id)
        session.add(dep)

    @property
    def event_dependencies(self):
        events = []
        deps = EventDependency.query.filter_by(event_id=self.id).all()
        for dep in deps:
            events.append(Event.query.filter_by(
                id=dep.event_dependency_id).first())
        return events

    def has_all_builds_in_state(self, state):
        """
        Returns True when all builds are in the given `state`.
        """
        return db.session.query(ArtifactBuild).filter_by(
            event_id=self.id).filter(state != state).count() == 0

    def builds_transition(self, state, reason):
        """
        Calls transition(state, reason) for all builds associated whit this
        event.
        """
        for build in self.builds:
            build.transition(state, reason)

    def __repr__(self):
        return "<Event %s, %r, %s>" % (self.message_id, self.event_type, self.search_key)

    def json(self):
        with app.app_context():
            event_url = flask.url_for('event', id=self.id)
            db.session.add(self)
            return {
                "id": self.id,
                "message_id": self.message_id,
                "search_key": self.search_key,
                "event_type_id": self.event_type_id,
                "url": event_url,
                "builds": [b.json() for b in self.builds],
            }


class EventDependency(FreshmakerBase):
    __tablename__ = "event_dependencies"
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)
    event_dependency_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)


class ArtifactBuild(FreshmakerBase):
    __tablename__ = "artifact_builds"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    original_nvr = db.Column(db.String, nullable=True)
    rebuilt_nvr = db.Column(db.String, nullable=True)
    type = db.Column(db.Integer)
    state = db.Column(db.Integer, nullable=False)
    state_reason = db.Column(db.String, nullable=True)
    time_submitted = db.Column(db.DateTime, nullable=False)
    time_completed = db.Column(db.DateTime)

    # Link to the Artifact on which this one depends and which triggered
    # the rebuild of this Artifact.
    dep_on_id = db.Column(db.Integer, db.ForeignKey('artifact_builds.id'))
    dep_on = relationship('ArtifactBuild', remote_side=[id])

    # Event associated with this Build
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'))
    event = relationship("Event", back_populates="builds")

    # Id of corresponding real build in external build system. Currently, it
    # could be ID of a build in MBS or Koji, maybe others in the future.
    # build_id may be NULL, which means this build has not been built in
    # external build system.
    build_id = db.Column(db.Integer)

    # Build args in json format.
    build_args = db.Column(db.String, nullable=True)

    @classmethod
    def create(cls, session, event, name, type,
               build_id=None, dep_on=None, state=None,
               original_nvr=None, rebuilt_nvr=None):

        now = datetime.utcnow()
        build = cls(
            name=name,
            original_nvr=original_nvr,
            rebuilt_nvr=rebuilt_nvr,
            type=type,
            event=event,
            state=state or ArtifactBuildState.BUILD.value,
            build_id=build_id,
            time_submitted=now,
            dep_on=dep_on
        )
        session.add(build)
        return build

    @validates('state')
    def validate_state(self, key, field):
        if field in [s.value for s in list(ArtifactBuildState)]:
            return field
        if field in [s.name.lower() for s in list(ArtifactBuildState)]:
            return ArtifactBuildState[field.upper()].value
        raise ValueError("%s: %s, not in %r" % (key, field, list(ArtifactBuildState)))

    @validates('type')
    def validate_type(self, key, field):
        if field in [t.value for t in list(ArtifactType)]:
            return field
        if field in [t.name.lower() for t in list(ArtifactType)]:
            return ArtifactType[field.upper()].value
        raise ValueError("%s: %s, not in %r" % (key, field, list(ArtifactType)))

    def depending_artifact_builds(self):
        """
        Returns list of artifact builds depending on this one.
        """
        return ArtifactBuild.query.filter_by(dep_on_id=self.id).all()

    def transition(self, state, state_reason):
        """
        Sets the state and state_reason of this ArtifactBuild.

        :param state: ArtifactBuildState value
        :param state_reason: Reason why this state has been set.
        """

        # Log the state and state_reason
        if state == ArtifactBuildState.FAILED.value:
            log_fnc = log.error
        else:
            log_fnc = log.info
        log_fnc("Artifact build %r moved to state %s, %r" % (
            self, ArtifactBuildState(state).name, state_reason))

        if self.state == state:
            return

        self.state = state
        self.state_reason = state_reason
        if self.state in [ArtifactBuildState.DONE.value,
                          ArtifactBuildState.FAILED.value,
                          ArtifactBuildState.CANCELED.value]:
            self.time_completed = datetime.utcnow()

        # For FAILED/CANCELED states, move also all the artifacts depending
        # on this one to FAILED/CANCELED state, because there is no way we
        # can rebuild them.
        if self.state in [ArtifactBuildState.FAILED.value,
                          ArtifactBuildState.CANCELED.value]:
            for build in self.depending_artifact_builds():
                build.transition(
                    self.state, "Cannot build artifact, because its "
                    "dependency cannot be built.")

    def __repr__(self):
        return "<ArtifactBuild %s, type %s, state %s, event %s>" % (
            self.name, ArtifactType(self.type).name,
            ArtifactBuildState(self.state).name, self.event.message_id)

    def json(self):
        build_args = {}
        if self.build_args:
            build_args = json.loads(self.build_args)

        with app.app_context():
            build_url = flask.url_for('build', id=self.id)
            db.session.add(self)
            return {
                "id": self.id,
                "name": self.name,
                "original_nvr": self.original_nvr,
                "rebuilt_nvr": self.rebuilt_nvr,
                "type": self.type,
                "type_name": ArtifactType(self.type).name,
                "state": self.state,
                "state_name": ArtifactBuildState(self.state).name,
                "state_reason": self.state_reason,
                "dep_on": self.dep_on.name if self.dep_on else None,
                "time_submitted": self.time_submitted,
                "time_completed": self.time_completed,
                "event_id": self.event_id,
                "build_id": self.build_id,
                "url": build_url,
                "build_args": build_args,
            }

    def get_root_dep_on(self):
        dep_on = self.dep_on
        while dep_on:
            dep = dep_on.dep_on
            if dep:
                dep_on = dep
            else:
                break
        return dep_on
