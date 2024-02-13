import re
import os
from enum import IntFlag as Enum
from copy import copy

import src.godot_types as ref
import ClassData as ClassData
from Tokenizer import Tokenizer


# recursive descent parser
class Parser:
	
	def __init__(self, filename, text, transpiler):
		# keep track of the script being transpiled
		self.script_name = filename
		
		# transpiler renamed 'out' for brevity
		self.out = transpiler
		
		# generator that splits text into tokens
		self.tokenizer = Tokenizer()
		self.tokens = self.tokenizer.tokenize(text)
		
		# update current token
		self.advance()
		
		# indentation level
		self.level = 0
		
		# script class data
		self.is_tool = None
		self.base_class = None
		self.class_name = None
		self.classData = None
		
		# local variable (name:type)
		self.locals = {}
	
	
	""" SCRIPT/STATEMENT GRAMMAR 
	
	transpile : [<Member>|<Method>|<Signal>]*
	
	Member -> <annotation>? <property>
	annotation -> @<name>[(*<params>)]?
	property -> [const|[static]? var] <name> [:<type>]? [<Assignment>]?
	Method -> func <name>(*<params>) [-> <type>]? :[Block]
	Signal -> signal name [(*params)]?
	Block -> <Statement>1+
	params -> <name> [: <type>]? [, params]*
	
	Statement
	 |->NoOperation     -> pass
	 |->Declaration     -> var <variable> <Assignment>
	 |->IfStatement     -> if <boolean>: <Block> [elif <boolean> <Block>]* [else <Block>]?
	 |->WhileStatement  -> while <boolean>: <Block>
	 |->ForStatement    -> for <variable> in <Expression> | : <Block>
	 |->MatchStatement  -> match <Expression>: [<Expression>:<Block>]1+                       ----> TODO
	 |->ReturnStatement -> return <Expression>
	 |->Assignment      -> <Expression> = <Expression>
	 |->Expression (see after statement implementation)
	
	Expression grammar defined later
	
	"""
	
	def transpile(self):
		# in case there is a file header / comments at start
		self.endline()
		
		# script start specific statements
		self.is_tool = self.expect('@') and self.expect('tool'); self.endline()
		self.base_class = self.consume() if self.expect('extends') else 'Object'; self.endline()
		self.class_name = self.consume() if self.expect('class_name') else self.script_name
		
		# initialize script class data
		self.classData = copy(ref.godot_types[self.base_class]) if self.base_class in ref.godot_types \
			else ClassData.ClassData()
		#print(self.classData.__dict__)
		
		# no endline after class name since we declare the class before that
		self.out.define_class(self.class_name, self.base_class, self.is_tool); self.endline()
		
		# script-level loop
		for i in range(2):
			
			for _ in self.doWhile(lambda:True):
				self.class_body()
			
			# get out if EOF reached
			if self.match_type('EOF'):
				print("reached EOF")
				break
			
			# panic system :
			# drop current line if we can't parse it
			
			token = self.current
			escaped = self.consumeUntil('LINE_END', separator=' ').replace('\n', '\\n')
			self.endline()
			
			msg = f'PANIC! <{escaped}> unexpected at {token}'
			self.out.line_comment(msg)
			print('---------', msg)
		
		# tell the transpiler we're done
		self.out.end_script()
	
	
	def class_body(self):
		static = self.expect('static')
		if self.expect('class'): self.nested_class()
		elif self.expect('enum'): self.enum()
		elif self.expect('func'): self.method(static)
		elif self.expect('signal'): self.signal()
		else: self.member(static)
		self.endline()
	
	
	def nested_class(self):
		class_name = self.consume()
		base_class = self.consume() if self.expect('extends') else 'Object'
		# NOTE: can inner classes be tools ? are they the same as their script class ?
		self.out.define_class(class_name, base_class, False)
		self.expect(':')
		
		self.level += 1
		class_lvl = self.level
		# NOTE: technically there would be no annotations in inner classes
		for _ in self.doWhile(lambda:self.level >= class_lvl): self.class_body()
		
		# TODO : add nested class to godot_types
	
	
	def enum(self):
		# NOTE: enums have similar syntax in gdscript, C# and cpp
		# lazily passing the enum definition as-is for now
		name = self.consume() if self.match_type('TEXT') else ''
		definition = self.consumeUntil('}')
		definition += self.consume() # get the remaining '}'
		self.out.enum(name, definition)
	
	
	DECL_FLAGS = Enum('DECL_FLAGS', ('none', 'static', 'constant', 'property', 'onready')) 
	
	# class member
	def member(self, static):
		
		onready = False
		
		# exports and such : @annotation[(params)]?
		# while is used to get out in case of on_ready
		annotation = None
		params = ''
		while self.expect('@'):
			
			# NOTE: special case for onready (needs moving the assignment into ready function)
			# TODO: call out.assignement with onready flag (later)
			if self.expect('on_ready'): onready = True; break
			
			annotation = self.consume()
			# NOTE: this should work for most cases
			if self.expect('('):
				params = self.consumeUntil(')').replace('"', '').replace("'", '')
				self.expect(')')
			self.endline()
		
		# member : [[static]? var|const] variable_name [: [type]? ]? = expression
		constant = self.expect('const')
		if constant or self.expect('var'):
			member = self.consume()
			if annotation: self.out.annotation(annotation, params, member)
			self.declare( member, \
					 self.DECL_FLAGS.property \
				| (  self.DECL_FLAGS.constant if constant \
				else self.DECL_FLAGS.static if static \
				else self.DECL_FLAGS.onready if onready \
				else self.DECL_FLAGS.none))
			# TODO: handle get set
	
	
	# Method -> func <name>(*<params>) [-> <type>]? :[Block]
	def method(self, static):
		name = self.consume()
		self.expect('(')
		
		params = {}
		params_init = {}
		
		# param -> <name> [: [<type>]?]? [= <Expression>]?
		for _ in self.doWhile(lambda: not self.expect(')')):
			pName = self.consume()
			pType = self.parseType() if self.expect(':') and self.match_type('TEXT') else None
			
			# initialisation
			if self.expect('='):
				pInit = self.expression()
				initType = next(pInit)
				pType = pType or initType
				params_init[pName] = pInit
			
			pType = pType or 'Variant'
			self.expect(',')
			params[pName] = pType
		
		# add params to locals
		for k,v in params.items(): self.locals[k] = v
		
		returnType = self.parseType() if self.expect('->') else None
		
		self.expect(':')
		
		# make transpiler write to a buffer
		# so we can parser block code, emit declaration then emit block code
		self.out.addLayer()
		
		blockType = self.Block()
		
		returnType = returnType or blockType
		self.classData.methods[name] = returnType
		
		self.out.define_method(name, params, params_init, returnType, static)
	
	
	def Block(self, addBreak = False):
		self.level += 1
		block_lvl = self.level
		return_type = None
		
		self.out.UpScope()
		
		for _ in self.doWhile(lambda : self.level >= block_lvl):
			res = self.statement()
			return_type = return_type or res
			self.endline(addBreak)
		
		return return_type
	
	def signal(self):
		name = self.consume()
		params = {}
		
		if self.expect('('):
			# param -> <name> [: [<type>]?]?
			# TODO: check if signal params can have initializers
			for _ in self.doWhile(lambda: not self.expect(')')):
				pName = self.consume()
				pType = self.parseType() if self.expect(':') and self.match_type('TEXT') else 'Variant'
				self.expect(',')
				params[pName] = pType
		
		self.out.define_signal(name, params)
	
	
	def statement(self):
		if   self.expect('pass'): return
		elif self.expect('var'): return self.declare(flags=self.DECL_FLAGS.none)
		elif self.expect('const'): return self.declare(flags=self.DECL_FLAGS.constant)
		elif self.expect('if'): return self.ifStmt()
		elif self.expect('while'): return self.whileStmt()
		elif self.expect('for'): return self.forStmt()
		elif self.expect('match'): return self.matchStmt()
		elif self.expect('return'):return self.returnStmt()
		elif self.expect('break'): return self.out.breakStmt();
		elif self.expect('continue'): return self.out.continueStmt()
		elif not self.match_type('LINE_END', 'COMMENT', 'LONG_STRING'): return self.reassign()
		return
	
	def ifStmt(self):
		cond = self.boolean(); next(cond)
		self.out.ifStmt(cond)
		
		self.expect(':')
		type = self.Block()
		
		while self.expect('elif'):
			cond2 = self.boolean(); next(cond2)
			self.out.elifStmt(cond2)
			
			self.expect(':')
			eliftype = self.Block()
			type = type or eliftype
		
		if self.expect('else') and self.expect(':'):
			self.out.elseStmt()
			elsetype = self.Block()
			type = type or elsetype

		return type
	
	def whileStmt(self):
		cond = self.boolean(); next(cond)
		self.out.whileStmt(cond)
		
		self.expect(':')
		type = self.Block()
		
		return type
	
	def forStmt(self):
		name = self.consume()
		
		self.expect('in')
		
		exp = self.expression()
		exp_type = next(exp)
		iterator_type = exp_type.replace('[]', '')
		
		self.locals[name] = iterator_type
		self.out.forStmt(name, iterator_type, exp)
		
		self.expect(':')
		type = self.Block()
		
		return type
	
	def matchStmt(self):
		
		switch_level = None
		return_type = None
		
		def evaluated():
			nonlocal switch_level
			expr = self.expression()
			type = next(expr)
			yield type
			next(expr)
			self.expect(':')
			self.level += 1
			switch_level = self.level
			yield type
		
		def cases(addBreak):
			nonlocal return_type
			for _ in self.doWhile(lambda:self.level >= switch_level):
				self.endline()
				default = self.expect('_')
				pattern = 'default' if default else self.expression()
				if not default: next(pattern)
				whenExpr = self.boolean() if self.expect('when') else None
				if whenExpr: next(whenExpr)
				self.expect(':')
				yield pattern, whenExpr
				blockType = self.Block(addBreak)
				return_type = return_type or blockType
		
		self.out.matchStmt(evaluated(), cases)
		
		return return_type
	
	def returnStmt(self):
		exp = self.expression()
		type = next(exp)
		self.out.returnStmt(exp)
		self.out.end_statement()
		return type
	
	def declare(self, name = None, flags = DECL_FLAGS.none):
		if not name: name = self.consume()
		type = self.parseType() if self.expect(':') and self.match_type('TEXT') else None
		
		# parsing assignment if needed
		ass = self.assignment( \
				name if flags & self.DECL_FLAGS.onready else None \
			) if self.expect('=') else None
		if ass:
			ass_type = next(ass)
			type = type or ass_type
		
		type = type or 'Variant'
		
		# emit code
		if flags & self.DECL_FLAGS.property:
			self.classData.members[name] = type
			self.out.declare_property(type, name, \
				flags & self.DECL_FLAGS.constant, \
				flags & self.DECL_FLAGS.static)
		else:
			self.locals[name] = type
			self.out.declare_variable(type, name)
		
		if ass: next(ass)
		self.out.end_statement()
	
	
	# reassign : <expression> = <expression>
	def reassign(self):
		# NOTE: expression() handles function calls and modification operators (a += b)
		# even though it is not conceptually correct
		exp = self.expression()
		exists = next(exp)
		
		if self.expect('='):
			ass = self.assignment()
			next(ass); next(exp); next(ass)
		else: next(exp)
		self.out.end_statement()
	
	
	""" EXPRESSION GRAMMAR
	
	all expressions (+ assignment sub-statement) use generators for type inference
	the format expected is :
		yield <type>
		<emit code>
		yield
	
	Expression		-> ternary
	ternary			-> [boolean [if boolean else boolean]* ]
	boolean			-> arithmetic [and|or|<|>|==|...  arithmetic]*
	arithmetic		-> [+|-|~] value [*|/|+|-|... value]*
	value			-> literal|subexpression|textCode
	literal			-> int|float|string|array|dict
	array			-> \[ [expresssion [, expression]*]? \]	
	dict			-> { [expresssion:expression [, expresssion:expression]*]? }
	subexpression	-> (expression)
	textCode		-> variable|reference|call|subscription
	variable		-> <name>
	reference		-> textCode.textCode
	call			-> textCode([*params]?)
	subscription	-> textcode[<index>]
	
	"""
	
	def assignment(self, onreadyName = None):
		exp = self.expression()
		yield next(exp);
		self.out.assignment(exp, onreadyName)
		yield
	
	
	def expression(self):
		exp = self.ternary()
		type = next(exp)
		if self.expect('as'): type = self.parseType()
		yield type or 'Variant'
		next(exp)
		yield
		
	
	def ternary(self):
		valTrue = self.boolean()
		yield next(valTrue)
		if self.expect('if'):
			# NOTE: nested ternary bug if passed in another way
			def impl():
				cond = self.boolean(); next(cond)
				yield next(cond)
				self.expect('else')
				valFalse = self.ternary(); next(valFalse)
				yield next(valTrue)
				yield next(valFalse)
			self.out.ternary(impl())
		else: next(valTrue)
		yield
	
	
	# mixing boolean and comparison for simplicity
	def boolean(self):
		ar1 = self.arithmetic()
		ar_type = next(ar1)
		
		op = self.consume() if self.match_type('COMPARISON') else None
		
		if op:
			ar2 = self.boolean()
			ar_type = next(ar2)
			yield 'bool'
			next(ar1)
			self.out.operator(op)
			next(ar2)
		else:
			yield ar_type
			next(ar1)
		yield
	
	
	def arithmetic(self):
		# unary operator ex: i = -4
		pre_op = self.consume() if self.match_type('UNARY') else None
		
		ar = self._arithmetic()
		yield next(ar);
		if pre_op: self.out.operator(pre_op)
		next(ar)
		yield
	
	
	def _arithmetic(self):
		value1 = self.value()
		value_type = next(value1)
		
		# NOTE: we accept arithmetic reassignment ex: i += 1
		# which is not exact but simpler to do this way
		op = self.consume() if self.match_type('ARITHMETIC') else None
		
		if op:
			value2 = self._arithmetic()
			value_type = next(value2)
			yield value_type
			next(value1)
			self.out.operator(op)
			next(value2)
		else:
			yield value_type
			next(value1)
		yield
	
	
	def value(self):
		
		# int
		if self.match_type('INT'):
			val = int(self.consume())
			yield 'int'
			self.out.literal(val)
			
		# float
		elif self.match_type('FLOAT'):
			val = float(self.consume())
			yield 'float'
			self.out.literal(val)
			
		# bool
		elif self.current.value in ('true', 'false'):
			val = self.consume() == 'true'
			yield 'bool'
			self.out.literal(val)
		
		# multiline (""") string
		elif self.match_type('LONG_STRING'):
			val = self.consume()
			yield 'string'
			self.out.literal(val)
		# "" or '' string
		elif self.match_type('STRING'):
			val = self.consume()
			yield 'string'
			self.out.literal(val)
			
		# array
		elif self.expect('['):
			yield 'Array'
			def iter():
				while not self.expect(']'):
					val = self.expression(); next(val)
					self.expect(',')
					yield val
			self.out.create_array(iter())
			
		# dictionary
		elif self.expect('{'):
			yield 'Dictionary'
			def iter():
				while not self.expect('}'):
					key = self.expression()
					val = self.expression()
					next(key); self.expect(':'); next(val)
					self.expect(',')
					yield (key, val)
			self.out.create_dict(iter())
			
		# subexpression : (expression)
		elif self.expect('('):
			enclosed = self.expression()
			yield next(enclosed)
			self.out.subexpression(enclosed)
			self.expect(')')
		
		# get_node shortcuts : $node => get_node("node") -> Node
		elif self.expect('$'):
			name = self.consume()
			yield 'Node'
			self.out.call("get_node", (passthrough(self.out.literal, name) ,) )
			
		# scene-unique nodes : %node => get_node("%node") -> Node
		elif self.expect('%'):
			name = self.consume()
			yield 'Node'
			self.out.call("get_node", (passthrough(self.out.literal, f'%{name}') ,) )
		
		# textCode : variable|reference|call
		elif self.match_type('TEXT'):
			content = self.textCode()
			yield next(content)
			next(content)
		else: yield
		yield
	
	
	# textCode : variable|call|reference
	def textCode(self):
		
		name = self.consume()
		
		# self. is essentially just syntactic in most languages
		this = name == 'self' and self.expect('.')
		if this: name = self.consume()
		
		# could be :
		# a member
		# a local
		# a singleton (ex: RenderingServer)
		# a global constant (ex: KEY_ESCAPE)
		singleton = name in ref.godot_types
		type = self.classData.members.get(name, None) \
			or self.locals.get(name, None) \
			or ref.godot_types['@GlobalScope'].constants.get(name, None) \
			or (name if singleton else None)
		
		# call
		if self.expect('('):
			call = self.call(name)
			yield next(call)
			if this: self.out.this()
			next(call)
		
		# reference
		elif self.expect('.'):
			reference = self.reference(type)
			yield next(reference)
			if this: self.out.this()
			self.out.singleton(name) if singleton else self.out.variable(name)
			next(reference)
		
		# subscription
		elif self.expect('['):
			s = self.subscription(type)
			yield next(s)
			if this: self.out.this()
			self.out.variable(name)
			next(s)
		
		# lone variable or global
		else:
			yield type
			if this: self.out.this()
			self.out.variable(name)
		yield
	
	
	def call(self, name, calling_type = None):
		
		# could be :
		# a constructor
		# a local method
		# a global function
		# another class's method
		constructor = name in ref.godot_types
		type = (name if constructor else None ) \
			or self.classData.methods.get(name, None) \
			or (ref.godot_types[calling_type].methods.get(name, None) if calling_type in ref.godot_types \
			else ref.godot_types['@GlobalScope'].methods.get(name, None) if not calling_type \
			else None)
		
		# determine params
		def iter():
			while not self.expect(')'):
				exp = self.expression(); next(exp); yield exp
				self.expect(',')
		params = ( *iter() ,)
		
		# emission of code 
		emit = lambda : \
			self.out.constructor(name, params) if constructor \
			else self.out.call(name, params)
		
		# reference
		if self.expect('.'):
			r = self.reference(type)
			yield next(r)
			emit()
			next(r)
		
		# subscription
		elif self.expect('['):
			s = self.subscription(type)
			yield next(s)
			emit()
			next(s)
		
		# end
		else:
			yield type
			emit()
		yield
	
	
	def reference(self, type):
		name = self.consume()
		# TODO: take classes defined locally into account
		member_type = ref.godot_types[type].members[name] if \
				type in ref.godot_types and name in ref.godot_types[type].members \
			else None
		
		# call
		if self.expect('('):
			call = self.call(name, type)
			yield next(call)
			# emit '.' while call() emits <name>(...)
			self.out.reference('') 
			next(call)
		
		# other reference
		elif self.expect('.'):
			r = self.reference(member_type)
			yield next(r)
			self.out.reference(name)
			next(r)
		
		# subscription
		elif self.expect('['):
			s = self.subscription(type)
			yield next(s)
			self.out.reference(name)
			next(s)
		
		# could be a constant
		elif type and name in ref.godot_types[type].constants:
			yield ref.godot_types[type].constants[name]
			self.out.constant(name)
		
		# end leaf
		else:
			yield member_type
			self.out.reference(name)
		yield
	
	
	def subscription(self, type):
		# NOTE: we only deternmine the type if it's a typed array
		type = type.replace('[]', '') if type and '[]' in type else None
		key = self.expression();next(key)
		self.expect(']')
		
		# call
		if self.expect('('):
			call = self.call('', type)
			yield next(call)
			next(call)
		
		# reference
		elif self.expect('.'):
			refer = self.reference(type)
			yield next(refer)
			self.out.subscription(key)
		
		# end leaf
		else:
			yield type
			self.out.subscription(key)
		yield
	
	
	""" parsing utils """
	
	def advance(self):
		try:
			self.current = next(self.tokens)
		except StopIteration as err:
			# reached end of file
			# using a trick to finish parsing
			self.current.type = 'EOF'
			self.current.value = 'EOF'
	
	# while implementation that avoids infinite loops
	def doWhile(self, condition):
		last = -1
		while self.current != last and condition():
			last = self.current; yield
	
	def match_type(self, *tokens):
		return any( (token == self.current.type for token in tokens) )
	
	def match_value(self, *tokens):
		return any( (token == self.current.value for token in tokens) )
	
	def expect(self, token):
		found = self.match_value(token)
		if found: self.advance()
		return found
	
	def consume(self):
		found = self.current.value
		self.advance()
		#print('+', found)
		return found
	
	# parse type string and format it the way godot docs do
	def parseType(self):
		type = self.consume()
		# Array[<type>] => <type>[]
		if type == 'Array' and self.expect('['):
			type = self.consume() + '[]'; self.expect(']')
		return type
		
	def consumeUntil(self, token, separator = ''):
		result = []
		for _ in self.doWhile(lambda : \
					not self.match_value(token) \
				and not self.match_type(token)):
			result.append(self.consume())
		return separator.join(result)
	
	
	# called when an endline is excpected
	# addBreak is for switch statements
	def endline(self, addBreak = False):
		lvl = -1
		jumpedLines = 0
		
		for _ in self.doWhile(lambda:self.match_type('LINE_END', 'COMMENT', 'LONG_STRING')):
			
			# stub
			emitComments = (lambda : None)
			
			# setting scope level only when we encounter non-whitespace
			if self.match_type('LINE_END'):
				lvl = int(self.consume())
				jumpedLines += 1
			
			# parse comments
			else:
				emit = self.out.line_comment if self.match_type('COMMENT') else self.out.multiline_comment
				content = self.consume()
				# override emitComments stub
				def emitComments(): emit(content)
			
			if self.match_type('LINE_END'):
				emitComments()
			
			# found code, indentation now matters
			# NOTE: for prettier output, we emit downscope directly
			else:
				if lvl!=-1:
					for i in range(self.level - lvl):
						if addBreak and i == 0: self.out += '\n'; self.out.breakStmt()
						self.level -=1
						self.out.DownScope();
						#self.out += f"      <downscope {self.current}>"
				emitComments()
				self.out += '\n' * jumpedLines
				jumpedLines = 0
		
		self.out += '\n' * jumpedLines

def passthrough(closure, *values):
	closure(*values); yield