'''
Created on Sep 26, 2013

@author: EHWAAL

Copyright (C) 2014 Evert van de Waal
This program is released under the conditions of the GNU General Public License.
'''

import model
import string
from urlparse import urlparse
import os.path
from model.config import currentFile
from PyQt4 import QtGui, QtCore
import sqlalchemy as sql
from primitives_2d import Block, Text, Line, NO_POS, extractSvgGradients
from connector import Connection
from styles import Styles, Style


# TODO: Allow addition of a child within view
# TODO: Ensure parents are below children
# TODO: Allow area select
# TODO: Allow copy - paste of blocks between views.
# TODO: Maak robuust voor meerdere instanties van het zelfde blok.
# TODO: Verwijderen van geselecteerde elementen met Delete knop.
# TODO: Add annotations (squares regions with a comment attached).
# TODO: Gebruik maken van de ConnectionRepresentation.

# FIXME: When dropping a new block or selecting a menu item, deselect the current items.
# FIXME: delete block in view leaves block outline on screen
# FIXME: rename architecture block is not shown in open viewer.
# FIXME: Zorg dat bij het aanmaken van iets nieuws, dit meteen geselecteerd is.

MIME_TYPE = 'application/x-qabstractitemmodeldatalist'

BLOCK_WIDTH  = 100
BLOCK_HEIGHT = 30


class NoDetailsFound(Exception):
  ''' Exception that is raised when looking for a widget at a certain place 
      and not finding anything.
  '''
  pass

class BlockItem(Block):
  ''' Representation of an architecture block.
  '''
  ROLE = 'archblock'
  def __init__(self, style, rep_details, block_details):
    self.details = rep_details
    self.block_details = block_details
    Block.__init__(self, rep_details, style, self.details.style_role, block_details.Name)
    self.applyStyle()
  def setRole(self, role):
    ''' Called by the style editing mechanism when the user changes the role. 
        The role is here only the user-determined part, and does not include the
        hard-coded part from ROLE.'''
    self.details.style_role = role
    Block.setRole(self, role)
    self.applyStyle()

class AnnotationItem(Block):
  ''' Representation of an annotation. '''
  ROLE = 'annotation'
  def __init__(self, style, details):
    # TODO: Take into account the anchor position
    self.details = details
    Block.__init__(self, details, style, details.style_role, details.Description)
    self.applyStyle()
  def setRole(self, role):
    ''' Called by the style editing mechanism when the user changes the role. 
        The role is here only the user-determined part, and does not include the
        hard-coded part from ROLE.'''
    self.details.style_role = role
    Block.setRole(self, role)
    self.applyStyle()
    
    
class FunctionPoint(Text):
  ROLE = 'functionpoint'
  def __init__(self, details, fp, anchor, style):
    text = '%s: %s'%(details.Order, fp.Name)
    role = details.style_role
    self.anchor = anchor
    self.details = details
    self.fp = fp
    self.arrow = None
    Text.__init__(self, text, style, role, apply=False)
    
    self.setFlag(QtGui.QGraphicsItem.ItemIsSelectable)
    self.setFlag(QtGui.QGraphicsItem.ItemIsMovable)
    
    if isinstance(self.anchor, BlockItem):
      self.arrow = None
    else:
      # Add the arrow.
      length = style.getFloat('%s-arrow-%s-length'%(role, self.ROLE), 1.0)
      self.arrow = Line(-10*length, 0, 0, 0, self, style, (role, self.ROLE))

    self.applyStyle()

  def applyStyle(self):
    Text.applyStyle(self)
    if self.arrow:
      self.arrow.applyStyle()
    self.updatePos()
  def setRole(self, role):
    ''' Called by the style editing mechanism when the user changes the role. 
        The role is here only the user-determined part, and does not include the
        hard-coded part from ROLE.'''
    self.details.style_role = role
    self.arrow.setRole(role)
    Text.setRole(self, role)
    self.applyStyle()
    
  def exportSvg(self):
    tmplt = '''
               <g transform="translate($x,$y)">
                 $arrow
                 $txt
               </g>
    '''
    arrow=self.arrow.exportSvg() if self.arrow else ''
    txt = Text.exportSvg(self, NO_POS)  # The text position is the position of the group.
    d = dict(text=str(self.text.toPlainText()),
             x=self.x(), y=self.y(),
             txt=txt,
             arrow = arrow)
    xml = string.Template(tmplt).substitute(d)
    return xml

  def anchorPos(self):
    x, y = self.anchor.fpPos()
    order = self.details.order_on_target
    x += 10*order
    y -= 20*order
    return x, y
    
  def updatePos(self):
    p = self.parentItem()
    text = '%s: %s'%(self.details.Order, self.fp.Name)
    self.setText(text)
    x, y = self.anchorPos()
    x += self.details.Xoffset
    y += self.details.Yoffset
    self.setPos(x,y)
    #print 'background rect:', p.rect(), p.pos(), self.pos()
    if self.arrow:
      angle = -self.anchor.line().angle()
      if self.fp.isResponse:
        angle += 180.0
      self.arrow.setRotation(angle)
      self.arrow.setPos(self.style.getOffset('%s-arrow-%s'%(self.role, self.ROLE), 
                                        default=[-10,10]))
      
  def mouseReleaseEvent(self, event):
    Text.mouseReleaseEvent(self, event)
    self.scene().session.commit()
      
  def mouseMoveEvent(self, event):
    # Get the current position
    p = self.pos()
    Text.mouseMoveEvent(self, event)
    
    # If moved, make it permanent
    if self.pos() != p:
      np = self.pos()
      self.details.Xoffset += np.x() - p.x()
      self.details.Yoffset += np.y() - p.y()



def getDetails(item, dont_raise=False):
  while item:
    if hasattr(item, 'details'):
      return item.details, item
    item = item.parentItem()
  if dont_raise:
    return None, None
  raise NoDetailsFound('Kon geen details vinden')
      

class MyScene(QtGui.QGraphicsScene):
  def __init__(self, details, drop2Details, session):
    '''
    drop2Details: a callback function that finds the details
    belonging to the item that was dropped.
    '''
    QtGui.QGraphicsScene.__init__(self)
    self.drop2Details = drop2Details
    self.session = session
    self.details = details

    # TODO: Remove all references to data bits except self.anchors
    self.connections = {}   # Connection.Id : Connection
    self.connection_items = {} # Connection.Id : connection item.
    self.all_details = []   # An ordered list of (fptoview, functionpoint) tuples
    self.known_fps = set()
    
    self.connectLine = None
    
    self.styles = Styles.style_sheet.getStyle(details.style)
    self.styles.subscribe(lambda _: self.applyStyle())
    
    # Add the existing blocks and connections
    self.anchors = {}    # Anchor.Id : Item tuples
    self.block_details = {} # ArchBlock.Id : ArchBlock
    self.block_items = {}   # ArchBlock.Id : BlockItem
    q = self.session.query(model.BlockRepresentation).order_by(model.BlockRepresentation.Order)
    blocks = q.filter(model.BlockRepresentation.View == details.Id).all()
    for block in blocks:
      self.anchors[block.Id] = block
      self.addBlock(block, add_connections=False)
      
    q = self.session.query(model.Annotation).order_by(model.Annotation.Order)
    annotations = q.filter(model.Annotation.View == details.Id).all()
    for a in annotations:
      self.addAnnotation(a)
    
    # Add connections that are relevant for this view.
    q = self.session.query(model.ConnectionRepresentation).\
      filter(model.ConnectionRepresentation.View==details.Id)
    for connection in q:
      self.addConnection(connection)
#
    # self.fp_details is sorted by 'order'.
    all_details = self.session.query(model.FpRepresentation, model.FunctionPoint).\
                     filter(model.FpRepresentation.View==self.details.Id).\
                     filter(model.FunctionPoint.Id == model.FpRepresentation.FunctionPoint).\
                     order_by(model.FpRepresentation.Order.asc()).all()
    
    self.processActions(all_details)
    self.fpviews = {}   # model.FpRepresentation : FunctionPoint
    
    for fpview, fpdetails in all_details:        
      self.addAction(fpview, fpdetails)
      
    sql.event.listen(model.FunctionPoint, 'after_update', self.onFpUpdate)
    sql.event.listen(model.FpRepresentation, 'after_update', self.onFp2UseCaseUpdate)
    sql.event.listen(model.Annotation, 'after_update', self.onAnnotationUpdate)
    
    self.sortBlocks(blocks)
    
  def close(self):
    ''' Called when the TwoDView closes. '''
    sql.event.remove(model.FunctionPoint, 'after_update', self.onFpUpdate)
    sql.event.remove(model.FpRepresentation, 'after_update', self.onFp2UseCaseUpdate)
    sql.event.remove(model.Annotation, 'after_update', self.onAnnotationUpdate)
    
  def applyStyle(self):
    ''' Re-apply the styles to all items. '''
    for i in self.items():
      if hasattr(i, 'applyStyle'):
        i.applyStyle()
  
  def sortFunctionPoints(self):
    ''' Sort the details in all_details. This is a list of (FpRepresentation, FunctionPoint) tuples.
        Sort them by the Order element of the FpRepresentation record.
    '''
    self.all_details = sorted(self.all_details, key=lambda x:x[0].Order)
    self.processActions(self.all_details)
    
  def sortBlocks(self, blocks):
    for count, b in enumerate(blocks):
      graphic = self.block_items[b.Id]
      graphic.setZValue(count)

  @staticmethod
  def processActions(all_details):
    ''' Determines how many actions are listed for each element.
    '''
    connections = {}
    blocks = {}
    for fp1, fp2 in all_details:
      connection = fp2.Connection
      block = fp2.Block
      if connection:
        others = connections.get(connection, 0)
        fp1.order_on_target = others
        connections[connection] = others + 1
      else:
        others = blocks.get(block, 0)
        fp1.order_on_target = others
        blocks[block] = others + 1
    

  def addAction(self, fpview, fpdetails, anchor_item = None):
    ''' Attach an action to the given item.
    
        fpview: an model.FpRepresentation instance.
        fpdetails: the associated model.FunctionPoint instance.
        anchor_item: A QGraphicsItem that is the parent for this fp.

    '''
    self.all_details.append((fpview, fpdetails))
    self.known_fps.add(fpdetails)
    self.processActions(self.all_details)
    fp = fpdetails
    # Find the anchor, if not already specified.
    if anchor_item is None:
      anchor_item = self.anchors[fpview.AnchorPoint]
    item = FunctionPoint(fpview, fp, anchor_item, self.styles)
    self.fpviews[fpview] = item
    self.addItem(item)

  def dragEnterEvent(self, event):
    if event.mimeData().hasFormat(MIME_TYPE):
      event.accept()
      
  def dragMoveEvent(self, event):
    if event.mimeData().hasFormat(MIME_TYPE):
      event.accept()
  
  def dropEvent(self, event):
    details = self.drop2Details(event)
    print 'got:', details

    coods = event.scenePos()
    
    if isinstance(details, model.ArchitectureBlock):
      new_details = model.BlockRepresentation(Block=details.Id,
                                              View = self.details.Id,
                                              x = coods.x(),
                                              y = coods.y(),
                                              height = BLOCK_HEIGHT,
                                              width = BLOCK_WIDTH,
                                              Order = len(self.anchors))
      self.session.add(new_details)
      self.session.commit()
      
      self.addBlock(new_details)
    
  def addBlock(self, rep_details, add_connections=True):
    coods = QtCore.QPointF(rep_details.x, rep_details.y)
    block_details = rep_details.theBlock
    block = BlockItem(self.styles, rep_details, block_details)
    self.anchors[rep_details.Id] = block
    self.addItem(block)
    block.setPos(coods)
    
    self.block_details[rep_details.Id] = block_details
    self.block_items[rep_details.Id] = block
        
  def addAnnotation(self, details):
    coods = QtCore.QPointF(details.x, details.y)
    block = AnnotationItem(self.styles, details)
    details.item = block
    self.addItem(block)
    block.setPos(coods)
    self.anchors[details.Id] = block
            
  def addConnection(self, connection_repr):
    # Find BlockRepr ids for all starts and ends
    start = self.anchors[connection_repr.Start]
    end = self.anchors[connection_repr.End]
    item = Connection(connection_repr, start, end, self.styles)
    item.setFlag(QtGui.QGraphicsItem.ItemIsSelectable)
    item.applyStyle()
    self.addItem(item)
    self.anchors[connection_repr.Id] = item
    self.connections[connection_repr.Id] = connection_repr
    self.connection_items[connection_repr.Id] = item
    
  def mouseMoveEvent(self, event):
    QtGui.QGraphicsScene.mouseMoveEvent(self, event)
    
    # Move the derived items when dragging
    if event.buttons() != QtCore.Qt.LeftButton:
      return
    
    items = self.selectedItems()

    if len(items) > 0:
      # Update the details without committing the changes
      for it in items:
        # Only update the model: the positions of the graphics item were already changed.
        it.updatePos()
        
      # Move function points as well.
      for d, it in self.fpviews.iteritems():
        if not it in items:
          it.updatePos()
        
        
  def onFpUpdate(self, mapper, connection, target):
    # Check if the detail is being shown
    if target not in self.known_fps:
      return
    for view, fp in self.all_details:
      if fp == target:
        self.fpviews[view].updatePos()

  def onFp2UseCaseUpdate(self, mapper, connection, target):
    # Check if the fp2uc is actually shown in this view
    if target not in self.fpviews:
      return
    self.fpviews[target].updatePos()
    
  def onAnnotationUpdate(self, mapper, connection, target):
    if hasattr(target, 'item'):
      target.item.text.setText(target.Description)

  def exportSvg(self):
    ''' Determine the SVG representation of this view,
        and return it as a string.
        
        String templates are filled-in to create the SVG code,
        no formal XML parsing is applied.
    '''
    # TODO: Order the items in their Z-order.
    tmpl = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
    <!-- Created with Archtool -->
    <svg    xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:cc="http://web.resource.org/cc/"
   xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:svg="http://www.w3.org/2000/svg"
   xmlns="http://www.w3.org/2000/svg">
   <g transform="translate($x, $y)">
      $gradients
      $lines
      $blocks
      $fps
      $annotations
    </g></svg>'''

    connections = [a for a in self.anchors.values() if isinstance(a, Connection)]
    annotations = [a for a in self.anchors.values() if isinstance(a, AnnotationItem)]
    gradients = extractSvgGradients(self.styles, BlockItem.ROLE)
    blocks = '\n'.join([b.exportSvg() for b in self.block_items.values()])
    fps    = '\n'.join([fp.exportSvg() for fp in self.fpviews.values()])
    lines  = '\n'.join([c.exportSvg() for c in connections])
    annotations = '\n'.join([a.exportSvg() for a in annotations])
    rect = self.sceneRect()
    x = -rect.x()
    y = -rect.y()
    result = string.Template(tmpl).substitute(locals())
    return result
    
      
      

def mkMenu(definition, parent):
  ''' Utility to create a menu from a configuration structure.'''
  menu = QtGui.QMenu(parent)
  for action, func in definition:
    if action == '---':
      menu.addSeparator()
    else:
      a = QtGui.QAction(action, parent)
      menu.addAction(a)
      a.triggered.connect(func)
  return menu


class TwoDView(QtGui.QGraphicsView):
  ''' The TwoDView renders the MyScene, showing the architecture view.
  '''
  selectedItemChanged = QtCore.pyqtSignal(object)
  def __init__(self, details, drop2Details, session):
    scene = MyScene(details, drop2Details, session)
    QtGui.QGraphicsView.__init__(self, scene)
    for hint in [QtGui.QPainter.Antialiasing, QtGui.QPainter.TextAntialiasing]:
      self.setRenderHint(hint)
    self.setAcceptDrops(True)
    #self.setContextMenuPolicy(QtCore.Qt.ActionsContextMenu)
    #a = QtGui.QAction('Nieuw Blok', self)
    #self.addAction(a)
    #a.triggered.connect(self.onAddBlock)
    # TODO: Implement copy to use case
    self.menu_noitem = mkMenu([('Nieuw Blok', self.onAddBlock),
                               ('Nieuwe Annotatie', self.onAddAnnotation),
                               ('Copieer naar Nieuw View', self.onCopyToUseCase),
                               ('Exporteer als SVG', self.exportSvg)], self)
    self.menu_action = mkMenu([('Eerder', self.onAdvance),
                    ('Later', self.onRetard),
                    ('---', None),
                    ('Detailleer Actie', self.onRefineAction),
                    ('Verwijder Actie', self.onDeleteItem)], self)
    self.scene = scene
    self.details = details
    self.last_rmouse_click = None
    self.session = session
    self.scene.selectionChanged.connect(self.onSelectionChanged)
    
  def close(self):
    ''' Overload of the QWidget close function. '''
    self.scene.close()
    QtGui.QGraphicsView.close(self)
    
  def mouseReleaseEvent(self, event):
    ''' The mouse press event is intercepted in order to remember the position
    of the right-click action, when adding an object using a right-click menu.
    '''
    print 'Mouses release at', event.pos()
    if event.button() == QtCore.Qt.RightButton:
      self.contextMenuEvent(event)
    else:
      # Forward left-click mouse events.
      QtGui.QGraphicsView.mouseReleaseEvent(self, event)
      
  def menuActionDetails(self, details, definition=None):
    ''' Construct the details for a right-click menu that
    allows the addition of function points.
    '''
    if definition is None:
      definition = []

    definition += [('Nieuwe Actie', self.onNewAction),
                  ('---', None)]
    def bind(n):
      ''' Utility: bind a menu action trigger to the right function '''
      return lambda: self.addExistingAction(n, details.Id)
    #for fp in details.FunctionPoints:
    for fp in details.FunctionPoints:
      definition.append((fp.Name, bind(fp.Id)))
    return definition
  
  def contextMenuEvent(self, event):
    ''' Called when the context menu is requested in the view.
        The function checks what the mouse was pointing at when the menu
        was requested, so the right menu can be shown.
    '''
    self.last_rmouse_click = event.pos()
    item = self.itemAt(event.pos())
    details, _ = getDetails(item, dont_raise=True)
    #print item.details, item.details.Name
    menu = None
    if item is None:
      menu = self.menu_noitem
    elif isinstance(details, model.ConnectionRepresentation):
      # Create the menu for a connection
      connection = details.theConnection
      definition = self.menuActionDetails(connection, [('Verbergen', self.onHideConnection),
                                                    ('---', None),
                                                    ('Verwijder Verbinding', self.onDeleteItem)])
      menu = mkMenu(definition, self)
    elif isinstance(details, model.FpRepresentation):
      # Use the action menu
      menu = self.menu_action
    else:
      # It must be an architecture block.
      # If two blocks are selected, allow them to be connected.
      if len(self.scene.selectedItems()) == 2:
        definition = [('Verbind blokken', self.onConnect)]
      else:
        block = self.scene.block_details[details.Id]
        definition = self.menuActionDetails(block, [('Move Up', self.onRetard),
                                                    ('Move Down', self.onAdvance),
                                                    ('---', None),
                                                    ('Verwijder Blok', self.onDeleteItem)])
      menu = mkMenu(definition, self)
    menu.exec_(self.mapToGlobal(event.pos()))
    event.accept()
    
  def onConnect(self):
    ''' Called when two blocks are connected '''
    items = self.scene.selectedItems()
    if len(items) != 2:
      return
    
    source, target = [getDetails(i)[0].Block for i in items]
    # Find the connection object, or create it if it does not yet exist.
    # This connection is between Architecture Blocks, not their representations.
    with model.sessionScope(self.session) as session:
      bc = model.BlockConnection
      conns = session.query(bc).\
                           filter(bc.Start.in_([source, target])).\
                           filter(bc.End.in_([source, target])).all()
      if conns:
        connection = conns[0]
      else:
        connection = model.BlockConnection(Start=source, End=target)
        session.add(connection)
        # We need a valid primary key, so flush the session but do not commit.
        session.flush()

      # Add the representation of the connection, between two representations of blocks.
      source, target = [getDetails(i)[0].Id for i in items]
      details = model.ConnectionRepresentation(Connection=connection.Id,
                                               Start=source,
                                               End=target)
      session.add(details)
      # Flush the database so that all references are updated.
      session.flush()
      self.scene.addConnection(details)
      self.scene.clearSelection()

  def onAddBlock(self, triggered):
    ''' Called to add a new block to the view. '''
    text, ok = QtGui.QInputDialog.getText(self, 'Nieuw Blok',
                                "Welke naam krijgt het blok?")
    if not ok:
      return
    
    text = str(text)
    block_details = model.ArchitectureBlock(Name=text, Parent=self.details.Parent)
    self.session.add(block_details)
    self.session.commit()
    pos = self.mapToScene(self.last_rmouse_click)
    repr_details = model.BlockRepresentation(Block=block_details.Id, View=self.details.Id, 
                                             x=pos.x(), y=pos.y(), 
                                             Order = len(self.scene.anchors),
                                             width=BLOCK_WIDTH, height=BLOCK_HEIGHT)
    self.session.add(repr_details)
    self.session.commit()
    self.scene.addBlock(repr_details)
    
  def onAddAnnotation(self, triggered=False):
    ''' Called to add a new annotation to the view. '''
    pos = self.mapToScene(self.last_rmouse_click)
    item = self.itemAt(self.last_rmouse_click)
    anchor = anchor_type = None
    x, y = self.last_rmouse_click.x(), self.last_rmouse_click.y()
    if item:
      anchor = item.details.Id
      anchor_type = item.details.__name__
      x = y = 0.0
    details = model.Annotation(View=self.details.Id,
                                x=x, y=y,
                                AnchorPoint=anchor,
                                AnchorType=anchor_type,
                                Order = len(self.scene.anchors),
                                width=BLOCK_WIDTH, height=BLOCK_HEIGHT)
    self.session.add(details)
    self.session.commit()
    self.scene.addAnnotation(details)
    
    
  def onDeleteItem(self):
    ''' Deletes the block located at the last known right-click
    location.
    '''
    item = self.itemAt(self.last_rmouse_click)
    details, item = getDetails(item)
    to_remove = [item]
    # Use a scoped session to ensure the consistency of the database.
    with model.sessionScope(self.session) as session:
      # When deleting a block, remove the connections as well.
      # Deleting a block means only the representation is removed from the view,
      # so remove only the line, leave the connection in the model.
      if isinstance(details, model.BlockRepresentation):
        block_id = details.Block
        for con in self.scene.connections.values():
          if con.Start == block_id or con.End == block_id:
            to_remove.append(self.scene.connection_items[con.Id])
      elif isinstance(details, model.Connection):
        session.delete(details)
        # When deleting connections, check that there are no functionpoints on it.
        nr_fps = self.session.query(sql.func.count(model.FunctionPoint.Id)).\
                        filter(model.FunctionPoint.Connection==details.Id).one()[0]
        if nr_fps > 0:
          raise RuntimeError('Can not delete connection: has function points!')
      session.delete(details)
    for it in to_remove:
      self.scene.removeItem(it)
      
  def onHideConnection(self):
    ''' Hide a connection. '''
    item = self.itemAt(self.last_rmouse_click)
    connection, item = getDetails(item)
    if not isinstance(connection, model.Connection):
      return
    
    # Check if there are any actions on this connection in this view
    actions = self.session.query(model.FpRepresentation, model.FunctionPoint.Connection).\
                   filter(model.FpRepresentation.View==self.details.Id).\
                   filter(model.FunctionPoint.Connection.Id==connection.Id).all()
    if len(actions) > 0:
      # Ask the user if he is sure.
      MB = QtGui.QMessageBox
      reply = MB.question(self, 'Weet u het zeker?',
                                 'De verbinding heeft acties in deze view. Toch verbergen?',
                                 MB.Yes, MB.No)
      if reply != MB.Yes:
        return

    # Start a transaction
    with model.sessionScope(self.session) as session:
      # Add the 'Hide' record.
      session.add(model.HiddenConnection(View=self.details.Id, Connection=connection.Id))
      # Delete any fp2uc records (leave the functionpoints!)
      for a in actions:
        session.delete(a)
      # Delete the connection item.
      self.scene.removeItem(item)
    
  def onNewAction(self):
    ''' Create a new action and add it to either a connection or a block. '''
    text, ok = QtGui.QInputDialog.getText(self, 'Nieuwe Actie',
                                "Welke naam krijgt de actie?")
    if not ok:
      return
    
    # Create the new action ('Function Point')
    item = self.itemAt(self.last_rmouse_click)
    details, item = getDetails(item)
    # Determine if adding an action to a block or to a connection.
    if isinstance(details, model.ConnectionRepresentation):
      fp = model.FunctionPoint(Name=str(text), Connection=details.Connection, Parent=None)
    else:
      fp = model.FunctionPoint(Name=str(text), Block=details.Block, Parent=None)
    self.session.add(fp)
    self.session.commit()
    # Also create the link to the FP in the view.
    self.addExistingAction(fp.Id, details.Id)
    
  def addExistingAction(self, fp_id, anchor_id):
    ''' Show an already existing action in the current view. '''
    order=self.session.query(sql.func.count(model.FpRepresentation.Id)).\
                             filter(model.FpRepresentation.View==self.details.Id).one()[0]
    fp = self.session.query(model.FunctionPoint).filter(model.FunctionPoint.Id==fp_id).one()
    fp2uc = model.FpRepresentation(FunctionPoint=fp_id, View=self.details.Id, Order=order,
                                   AnchorPoint=anchor_id)
    self.session.add(fp2uc)
    self.session.commit()
    _, item = getDetails(self.itemAt(self.last_rmouse_click))
    self.scene.addAction(fp2uc, fp, item)
    
  def onAdvance(self, triggered, retard=False):
    ''' Called to move either a block or an action forward. '''
    item, _ = getDetails(self.itemAt(self.last_rmouse_click))
    cls = item.__class__
    items = self.session.query(cls).\
                               filter(cls.View==item.View).\
                               order_by(cls.Order).all()
    # Find out where this item is
    index = items.index(item)
    if not retard:
      # Check if it is already the first item
      if index == 0:
        return
      # Swap them
      items[index], items[index-1] = items[index-1], items[index]
    else:
      # Check if it already is the final item
      if index == len(items)-1:
        return
      # Swap the relevant items
      items[index], items[index+1] = items[index+1], items[index]
    # Normalize the orders
    for count, it in enumerate(items):
      it.Order = count
    self.session.commit()
    if cls == model.FpRepresentation:
      self.scene.sortFunctionPoints()
    else:
      self.scene.sortBlocks(items)
      
  def onRetard(self, triggered):
    ''' Called to move either a block or an action backwards. '''
    self.onAdvance(triggered, retard=True)

  def normalizeActions(self):
    ''' Cause the order field for the actions in the view to be normalised.'''
    actions = self.session.query(model.FpRepresentation).\
                               filter(model.FpRepresentation.View==self.details.Id).\
                               order_by(model.FpRepresentation.Order.asc()).all()
    for count, details in enumerate(actions):
      details.Order = count
    self.session.commit()
    
  def onRefineAction(self):
    ''' Called when the user wants to refine an action in a Use Case. '''
    fpview, _ = getDetails(self.itemAt(self.last_rmouse_click))
    # Add a new view that refers to the indicated function point
    fp = fpview.FunctionPoint
    view = fpview.View
    
    new_view = model.View(Name=fp.Name, Parent=view, Refinement=fp)
    self.session.add(new_view)
    # TODO: Cause the new view to be opened!
    
  def onCopyToUseCase(self):
    ''' Called to copy a view, to be the basis for a new view. '''
    # TODO: Implement copy to use case
    pass
  
  
  def onSelectionChanged(self):
    ''' Called when the items that are selected is changed.
        Causes a signal to be published so that the details viewer can be uipdated.'''
    # Check one item is selected.
    items = self.scene.selectedItems()
    if len(items) == 1:
      details = items[0].details
      if details.__class__ == model.BlockRepresentation:
        details = self.scene.block_details[details.Id]
      elif details.__class__ == model.FpRepresentation:
        details = self.session.query(model.FunctionPoint).\
                       filter(model.FunctionPoint.Id == details.FunctionPoint).one()
      self.selectedItemChanged.emit(details)
    Style.current_style.set(self.scene.styles)
    Style.current_object.set(items)

  def exportSvg(self):
    ''' Called when the user wants to export a view as SVG file.
        The SVG is stored as a file containing the model and view names.
    '''
    svg = self.scene.exportSvg()
    path = self.details.getParents()
    model_url = currentFile()
    model_name = urlparse(model_url)[2]
    dirname, basename = os.path.split(model_name)
    fname = '%s.%s.svg'%(basename, '.'.join([p.Name for p in path]))
    fname = os.path.join(dirname, fname)
    with open(fname, 'w') as f:
      f.write(svg)
    QtGui.QMessageBox.information(self, 'SVG Exported',
                                 'The diagram was exported as %s'%fname)

